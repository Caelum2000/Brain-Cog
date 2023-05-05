import torch
import torch.nn as nn
from braincog.base.connection.layer import SMaxPool, LIPool
from .merge import mergeConvBN
from .spicalib import SpiCalib
import types
import os
import sys

layer_index = 0  # layer index for SNode


class FolderPath:
    folder_path = "init"

class HookScale(nn.Module):
    """ 在每个ReLU层后记录该层的百分位最大值

    For channelnorm: 获取最大值时使用了torch.quantile
    For layernorm：  使用sort，然后手动取百分比，因为quantile在计算单个通道时有上限，batch较大时易出错
    """

    def __init__(self,
                 p: float = 0.9995,
                 channelnorm: bool = False,
                 gamma: float = 0.999,
                 ):
        super().__init__()
        if channelnorm:
            self.register_buffer('scale', torch.tensor(0.0))
        else:
            self.register_buffer('scale', torch.tensor(0.0))

        self.p = p
        self.channelnorm = channelnorm
        self.gamma = gamma

    def forward(self, x):
        x = torch.where(x.detach() < self.gamma, x.detach(),
                        torch.tensor(self.gamma, dtype=x.dtype, device=x.device))
        if len(x.shape) == 4 and self.channelnorm:
            num_channel = x.shape[1]
            tmp = torch.quantile(x.permute(1, 0, 2, 3).reshape(num_channel, -1), self.p, dim=1,
                                 interpolation='lower') + 1e-10
            self.scale = torch.max(tmp, self.scale)
        else:
            sort, _ = torch.sort(x.view(-1))
            self.scale = torch.max(sort[int(sort.shape[0] * self.p) - 1], self.scale)
        return x


class Hookoutput(nn.Module):
    """
    在伪转换中为ReLU和ClipQuan提供包装，用于监控其输出
    """

    def __init__(self, module):
        super(Hookoutput, self).__init__()
        self.activation = 0.
        self.operation = module

    def forward(self, x):
        output = self.operation(x)
        self.activation = output.detach()
        return output


class Scale(nn.Module):
    """
    对前向过程的值进行缩放
    """

    def __init__(self, scale: float = 1.0):
        super().__init__()
        self.register_buffer('scale', scale)

    def forward(self, x):
        if len(self.scale.shape) == 1:
            return self.scale.unsqueeze(0).unsqueeze(2).unsqueeze(3).expand_as(x) * x
        else:
            return self.scale * x


def reset(self):
    """
    转换的网络来自ANN，需要将新附加上的脉冲module进行reset
    判断module名称并调用各自节点的reset方法
    """
    children = list(self.named_children())
    for i, (name, child) in enumerate(children):
        if isinstance(child, (SNode, LIPool, SMaxPool)):
            child.reset()
        else:
            reset(child)


class Convertor(nn.Module):
    """ANN2SNN转换器

    用于转换完整的pytorch模型，使用dataloader中部分数据进行最大值计算，通过p控制获取第p百分比最大值

    channlenorm: https://arxiv.org/abs/1903.06530
    channelnorm可以对每个通道获取最大值并进行权重归一化

    gamma: https://arxiv.org/abs/2204.13271
    gamma可以控制burst spikes的脉冲数，burst spike可以提高神经元的脉冲发放能力，减小信息残留

    lipool: https://arxiv.org/abs/2204.13271
    lipool用于使用侧向抑制机制进行最大池化，LIPooling能够对SNN中的最大池化进行有效的转换

    soft_mode: https://arxiv.org/abs/1612.04052
    soft_mode被称为软重置，可以减小重置过程神经元的信息损失，有效提高转换的性能

    merge用于是否对网络中相邻的卷积和BN层进行融合
    batch_norm控制对dataloader的数据集的用量
    """

    def __init__(self,
                 dataloader,
                 device=None,
                 p=0.9995,
                 channelnorm=False,
                 lipool=True,
                 gamma=1,
                 soft_mode=True,
                 merge=True,
                 batch_num=1,
                 spicalib=0,
                 useDET=False, useDTT=False, useSC=None
                 ):
        super(Convertor, self).__init__()
        self.dataloader = dataloader
        self.device = device
        self.p = p
        self.channelnorm = channelnorm
        self.lipool = lipool
        self.gamma = gamma
        self.soft_mode = soft_mode
        self.merge = merge
        self.batch_num = batch_num
        self.spicalib = spicalib
        self.useDET = useDET
        self.useDTT = useDTT
        self.useSC = useSC

    def forward(self, model):
        model.eval()
        model = Convertor.register_hook(model, self.p, self.channelnorm, self.gamma)
        model = Convertor.get_percentile(model, self.dataloader, self.device, batch_num=self.batch_num)
        model = mergeConvBN(model) if self.merge else model
        model = Convertor.replace_for_spike(model, self.lipool, self.soft_mode, self.gamma, self.spicalib, self.useDET,
                                            self.useDTT, self.useSC)
        model.reset = types.MethodType(reset, model)
        return model

    @staticmethod
    def register_hook(model, p=0.99, channelnorm=False, gamma=0.999):
        """ Reference: https://github.com/fangwei123456/spikingjelly

        将网络的每一层后注册一个HookScale类
        该方法在仿真上等效于与对权重进行归一化操作，且易扩展到任意结构的网络中
        """
        children = list(model.named_children())
        for _, (name, child) in enumerate(children):
            if isinstance(child, nn.ReLU):
                model._modules[name] = nn.Sequential(nn.ReLU(), HookScale(p, channelnorm, gamma))
            else:
                Convertor.register_hook(child, p, channelnorm, gamma)
        return model

    @staticmethod
    def get_percentile(model, dataloader, device, batch_num=1):
        """
        该函数需与具有HookScale层的网络配合使用
        """
        for idx, (data, _) in enumerate(dataloader):
            data = data.to(device)
            if idx >= batch_num:
                break
            model(data)
        return model

    @staticmethod
    def replace_for_spike(model, lipool=True, soft_mode=True, gamma=1, spicalib=0, useDET=False, useDTT=False, useSC=None):
        """
        该函数用于将定义好的ANN模型转换为SNN模型
        ReLU单元将被替换为脉冲神经元，
        如果模型中使用了最大池化，lipool参数将定义使用常规模型还是LIPooling方法
        """
        children = list(model.named_children())
        for _, (name, child) in enumerate(children):
            if isinstance(child, nn.Sequential) and len(child) == 2 and isinstance(child[0], nn.ReLU) and isinstance(child[1], HookScale):
                global layer_index
                model._modules[name] = nn.Sequential(
                    Scale(1.0 / child[1].scale),
                    SNode(soft_mode, gamma, useDET=useDET, useDTT=useDTT, useSC=useSC, layer_index=layer_index),
                    SpiCalib(spicalib),
                    Scale(child[1].scale)
                )
                layer_index += 1
            if isinstance(child, nn.MaxPool2d):
                model._modules[name] = LIPool(child) if lipool else SMaxPool(child)
            else:
                Convertor.replace_for_spike(child, lipool, soft_mode, gamma, useDET=useDET, useDTT=useDTT, useSC=useSC)
        return model


class SNode(nn.Module):
    """
    用于转换后的SNN的神经元模型
    IF神经元模型由gamma=1确定，当gamma为其他大于1的值时，即为使用burst神经元模型
    soft_mode用于定义神经元的重置方法，soft重置能够极大地减少神经元在重置过程的信息损失
    """

    def __init__(self, soft_mode=False, gamma=5, useDET=False, useDTT=False, useSC=None, layer_index=1):
        super(SNode, self).__init__()
        self.threshold = 1.0
        self.maxThreshold = 1.0
        self.soft_mode = soft_mode
        self.gamma = gamma

        self.mem = 0
        self.spike = 0

        self.Vm = 0.
        self.summem = 0.
        self.t = 0
        self.all_spike = 0
        self.V_T = 0
        self.useDET = useDET
        self.useDTT = useDTT
        self.useSC = useSC
        self.layer_index = layer_index
        # hyperparameters
        self.alpha = 0
        self.ka = 0
        self.ki = 0
        self.C = 0
        self.tau_mp = 0
        self.tau_rd = 0

        # record sin
        self.mem_16 = 0.0
        self.spike_mask = 0
        self.sin_spikenum = 0.0
        self.sin_ratio = []  # snn中sin占负的比例
        self.last_spike = 0
        self.confidence = []
        self.neg_ratio = []  # ann中负的占总的比例
        self.sin_all_ratio = []  # snn中sin占总的比例
        self.pos_all_ratio = []  # snn中pos占总的比例
        self.should_all_ratio = []  # snn中pos应该发但是没发占总的比例
        self.confidence = []  # snn中sin占所有发的比例
        self.avg_error_spikenum = []  # snn中错发的平均个数

    def forward(self, x):
        self.mem = self.mem + x
        self.spike = torch.zeros_like(x)
        if self.t == 0:
            self.threshold = torch.full(x.shape, 1.0 * self.maxThreshold).to(x.device)
            self.V_T = -torch.full(x.shape, self.maxThreshold).to(x.device)
            # init hyperparameters
            hp = []
            path = FolderPath.folder_path.split('/')
            path = os.path.join(path[0], path[1], path[2], 'hyperparameters.txt')
            with open(path, 'r') as f:
                data = f.readlines()  # 将txt中所有字符串读入data
                for ind, line in enumerate(data):
                    numbers = line.split()  # 将数据分隔
                    hp.append(list(map(float, numbers))[0])  # 转化为浮点数

            self.alpha = hp[0]
            self.ka = hp[1]
            self.ki = hp[2]
            self.C = hp[3]
            self.tau_mp = hp[4]
            self.tau_rd = hp[5]
        else:
            DTT = self.tau_mp * (self.alpha * (self.last_mem - self.Vm) + self.V_T + self.ka * torch.log(
                1 + torch.exp((self.last_mem - self.Vm) / self.ki)))
            DET = self.tau_rd * torch.exp(-1 * x / self.C)
            if self.useDET is True and self.useDTT is True:
                self.threshold = DET + DTT
            elif self.useDTT is True:
                self.threshold = DTT
            elif self.useDET is True:
                self.threshold = DET
            else:
                print("wrong logics")
                sys.exit()
            self.threshold = torch.sigmoid(self.threshold)
            self.threshold *= self.maxThreshold

        self.spike = (self.mem / self.threshold).floor().clamp(min=0, max=self.gamma)
        self.soft_reset() if self.soft_mode else self.hard_reset

        if self.useSC is True:
            if self.t < 16:
                # read confidence
                path = FolderPath.folder_path.split('/')
                path = os.path.join(path[0], path[1], path[2], 'neuron_confidence_vgg16.txt')
                with open(path, 'r') as f:
                    data = f.readlines()  # 将txt中所有字符串读入data
                    for ind, line in enumerate(data):
                        numbers = line.split()  # 将数据分隔
                        self.confidence.append(list(map(float, numbers))[0])  # 转化为浮点数
                mask = (torch.rand(x.shape) >= (1.0 - self.confidence[self.layer_index])).float().cuda()
                self.spike = self.spike * mask  # random drop
        self.all_spike += self.spike
        out = self.spike * self.threshold
        self.t += 1
        self.last_mem = self.mem
        self.summem += self.mem
        self.Vm = (self.summem / self.t)
        return out

    def hard_reset(self):
        """
        硬重置后神经元的膜电势被重置为0
        """
        self.mem = self.mem * (1 - self.spike.detach())

    def soft_reset(self):
        """
        软重置后神经元的膜电势为神经元当前膜电势减去阈值
        """
        self.mem = self.mem - self.threshold * self.spike.detach()

    def reset(self):
        self.mem = 0
        self.spike = 0
        self.maxThreshold = 1.0
