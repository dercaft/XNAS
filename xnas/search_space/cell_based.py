""" Cell based search space """
import torch
import torch.nn as nn
import numpy as np
import torch.nn.functional as F
from collections import namedtuple

OPS_ = {
    'none': lambda C_in, C_out, stride, affine: Zero(stride),
    'avg_pool_3x3': lambda C_in, C_out, stride, affine: PoolBN('avg', C_in, 3, stride, 1, affine=affine),
    'max_pool_3x3': lambda C_in, C_out, stride, affine: PoolBN('max', C_in, 3, stride, 1, affine=affine),
    'skip_connect': lambda C_in, C_out, stride, affine: Identity() if stride == 1 else FactorizedReduce(C_in, C_out, affine=affine),
    'sep_conv_3x3': lambda C_in, C_out, stride, affine: SepConv(C_in, C_out, 3, stride, 1, affine=affine),
    'sep_conv_5x5': lambda C_in, C_out, stride, affine: SepConv(C_in, C_out, 5, stride, 2, affine=affine),
    'sep_conv_7x7': lambda C_in, C_out, stride, affine: SepConv(C_in, C_out, 7, stride, 3, affine=affine),
    # 5x5
    'dil_conv_3x3': lambda C_in, C_out, stride, affine: DilConv(C_in, C_out, 3, stride, 2, 2, affine=affine),
    # 9x9
    'dil_conv_5x5': lambda C_in, C_out, stride, affine: DilConv(C_in, C_out, 5, stride, 4, 2, affine=affine),
    'conv_7x1_1x7': lambda C_in, C_out, stride, affine: FacConv(C_in, C_out, 7, stride, 3, affine=affine),
    'nor_conv_3x3': lambda C_in, C_out, stride, affine: ReLUConvBN(C_in, C_out, 3, stride, 1, affine=affine),
    'nor_conv_1x1': lambda C_in, C_out, stride, affine: ReLUConvBN(C_in, C_out, 1, stride, 0, affine=affine),
}

NON_PARAMETER_OP = ['none', 'avg_pool_3x3', 'max_pool_3x3', 'skip_connect']
PARAMETER_OP = ['sep_conv_3x3', 'sep_conv_5x5', 'sep_conv_7x7', 'dil_conv_3x3',
                'dil_conv_5x5', 'conv_7x1_1x7', 'nor_conv_3x3', 'nor_conv_1x1']
GDAS_OP = ['none', 'skip_connect', 'avg_pool_3x3', 'max_pool_3x3',
            'dil_conv_3x3', 'dil_conv_5x5', 'sep_conv_3x3', 'sep_conv_5x5']

def get_op_index(op_list, parameter_list):
    op_idx_list = []
    for op_idx, op in enumerate(op_list):
        if op in parameter_list:
            op_idx_list.append(op_idx)
    return op_idx_list


def darts_weight_unpack(weight, n_nodes, input_nodes=2):
    """
        Unpack 2d weight matrix to dag
    """
    w_dag = []
    start_index = 0
    end_index = input_nodes
    for i in range(n_nodes):
        w_dag.append(weight[start_index:end_index])
        start_index = end_index
        end_index += input_nodes + i + 1
    return w_dag
def gdas_indexes_unpack(weight,n_nodes,input_nodes=2):
    """
        Unpack 1d indexes list to dag
    """
    w_dag = []
    start_index = 0
    end_index = input_nodes
    for i in range(n_nodes):
        w_dag.append(weight[start_index:end_index])
        start_index = end_index
        end_index += input_nodes + i + 1
    return w_dag    

def parse_from_numpy(alpha, k, basic_op_list=None):
    """
    parse continuous alpha to discrete gene.
    alpha is ParameterList:
    ParameterList [
        Parameter(n_edges1, n_ops),
        Parameter(n_edges2, n_ops),
        ...
    ]

    gene is list:
    [
        [('node1_ops_1', node_idx), ..., ('node1_ops_k', node_idx)],
        [('node2_ops_1', node_idx), ..., ('node2_ops_k', node_idx)],
        ...
    ]
    each node has two edges (k=2) in CNN.
    """

    gene = []
    assert basic_op_list[-1] == 'none'  # assume last PRIMITIVE is 'none'

    # 1) Convert the mixed op to discrete edge (single op) by choosing top-1 weight edge
    # 2) Choose top-k edges per node by edge score (top-1 weight in edge)
    for edges in alpha:
        # edges: Tensor(n_edges, n_ops)
        edge_max, primitive_indices = torch.topk(
            torch.tensor(edges[:, :-1]), 1)  # ignore 'none'
        topk_edge_values, topk_edge_indices = torch.topk(edge_max.view(-1), k)
        node_gene = []
        for edge_idx in topk_edge_indices:
            prim_idx = primitive_indices[edge_idx]
            prim = basic_op_list[prim_idx]
            node_gene.append((prim, edge_idx.item()))

        gene.append(node_gene)

    return gene


class ReLUConvBN(nn.Module):

    def __init__(self, C_in, C_out, kernel_size, stride, padding, affine=True):
        super(ReLUConvBN, self).__init__()
        self.op = nn.Sequential(
            nn.ReLU(inplace=False),
            nn.Conv2d(C_in, C_out, kernel_size, stride=stride,
                      padding=padding, bias=False),
            nn.BatchNorm2d(C_out, affine=affine)
        )

    def forward(self, x):
        return self.op(x)


def drop_path_(x, drop_prob, training):
    if training and drop_prob > 0.:
        keep_prob = 1. - drop_prob
        # per data point mask; assuming x in cuda.
        mask = torch.cuda.FloatTensor(x.size(0), 1, 1, 1).bernoulli_(keep_prob)
        x.div_(keep_prob).mul_(mask)
    return x


class DropPath_(nn.Module):
    def __init__(self, p=0.):
        """ [!] DropPath is inplace module
        Args:
            p: probability of an path to be zeroed.
        """
        super().__init__()
        self.p = p

    def extra_repr(self):
        return 'p={}, inplace'.format(self.p)

    def forward(self, x):
        drop_path_(x, self.p, self.training)

        return x


class PoolBN(nn.Module):
    """
    AvgPool or MaxPool - BN
    """

    def __init__(self, pool_type, C, kernel_size, stride, padding, affine=True):
        """
        Args:
            pool_type: 'max' or 'avg'
        """
        super().__init__()
        if pool_type.lower() == 'max':
            self.pool = nn.MaxPool2d(kernel_size, stride, padding)
        elif pool_type.lower() == 'avg':
            self.pool = nn.AvgPool2d(
                kernel_size, stride, padding, count_include_pad=False)
        else:
            raise ValueError()

        self.bn = nn.BatchNorm2d(C, affine=affine)

    def forward(self, x):
        out = self.pool(x)
        out = self.bn(out)
        return out

class POOLING(nn.Module):

  def __init__(self, C_in, C_out, stride, mode, affine=True, track_running_stats=True):
    super(POOLING, self).__init__()
    if C_in == C_out:
      self.preprocess = None
    else:
      self.preprocess = ReLUConvBN(C_in, C_out, 1, 1, 0, 1, affine, track_running_stats)
    if mode == 'avg'  : self.op = nn.AvgPool2d(3, stride=stride, padding=1, count_include_pad=False)
    elif mode == 'max': self.op = nn.MaxPool2d(3, stride=stride, padding=1)
    else              : raise ValueError('Invalid mode={:} in POOLING'.format(mode))

  def forward(self, inputs):
    if self.preprocess: x = self.preprocess(inputs)
    else              : x = inputs
    return self.op(x)
 

class StdConv(nn.Module):
    """ Standard conv
    ReLU - Conv - BN
    """

    def __init__(self, C_in, C_out, kernel_size, stride, padding, affine=True):
        super().__init__()
        self.net = nn.Sequential(
            nn.ReLU(),
            nn.Conv2d(C_in, C_out, kernel_size, stride, padding, bias=False),
            nn.BatchNorm2d(C_out, affine=affine)
        )

    def forward(self, x):
        return self.net(x)


class FacConv(nn.Module):
    """ Factorized conv
    ReLU - Conv(Kx1) - Conv(1xK) - BN
    """

    def __init__(self, C_in, C_out, kernel_length, stride, padding, affine=True):
        super().__init__()
        self.net = nn.Sequential(
            nn.ReLU(),
            nn.Conv2d(C_in, C_in, (kernel_length, 1),
                      stride, padding, bias=False),
            nn.Conv2d(C_in, C_out, (1, kernel_length),
                      stride, padding, bias=False),
            nn.BatchNorm2d(C_out, affine=affine)
        )

    def forward(self, x):
        return self.net(x)


class DilConv(nn.Module):
    """ (Dilated) depthwise separable conv
    ReLU - (Dilated) depthwise separable - Pointwise - BN

    If dilation == 2, 3x3 conv => 5x5 receptive field
                      5x5 conv => 9x9 receptive field
    """

    def __init__(self, C_in, C_out, kernel_size, stride, padding, dilation, affine=True):
        super().__init__()
        self.net = nn.Sequential(
            nn.ReLU(),
            nn.Conv2d(C_in, C_in, kernel_size, stride, padding, dilation=dilation, groups=C_in,
                      bias=False),
            nn.Conv2d(C_in, C_out, 1, stride=1, padding=0, bias=False),
            nn.BatchNorm2d(C_out, affine=affine)
        )

    def forward(self, x):
        return self.net(x)


class SepConv(nn.Module):
    """ Depthwise separable conv
    DilConv(dilation=1) * 2
    """

    def __init__(self, C_in, C_out, kernel_size, stride, padding, affine=True):
        super().__init__()
        self.net = nn.Sequential(
            DilConv(C_in, C_in, kernel_size, stride,
                    padding, dilation=1, affine=affine),
            DilConv(C_in, C_out, kernel_size, 1,
                    padding, dilation=1, affine=affine)
        )

    def forward(self, x):
        return self.net(x)


class Identity(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, x):
        return x


class Zero(nn.Module):
    def __init__(self, stride):
        super().__init__()
        self.stride = stride

    def forward(self, x):
        if self.stride == 1:
            return x * 0.

        # re-sizing by stride
        return x[:, :, ::self.stride, ::self.stride] * 0.


class FactorizedReduce(nn.Module):
    """
    Reduce feature map size by factorized pointwise(stride=2).
    """

    def __init__(self, C_in, C_out, affine=True):
        super().__init__()
        self.relu = nn.ReLU()
        self.conv1 = nn.Conv2d(C_in, C_out // 2, 1,
                               stride=2, padding=0, bias=False)
        self.conv2 = nn.Conv2d(C_in, C_out // 2, 1,
                               stride=2, padding=0, bias=False)
        self.bn = nn.BatchNorm2d(C_out, affine=affine)

    def forward(self, x):
        x = self.relu(x)
        out = torch.cat([self.conv1(x), self.conv2(x[:, :, 1:, 1:])], dim=1)
        out = self.bn(out)
        return out


'''
Basic operation of the cell based search space
'''


class _MixedOp(nn.Module):
    """ define the basic search space operation according to string """

    def __init__(self, C_in, C_out, stride, basic_op_list=None):
        super().__init__()
        self._ops = nn.ModuleList()
        assert basic_op_list is not None, "the basic op list cannot be none!"
        basic_primitives = basic_op_list
        for primitive in basic_primitives:
            op = OPS_[primitive](C_in, C_out, stride, affine=False)
            self._ops.append(op)

    def forward(self, x, weights):
        """
        Args:
            x: input
            weights: weight for each operation
        """
        assert len(self._ops) == len(weights)
        _x = []
        for i, value in enumerate(weights):
            if value == 1:
                _x.append(self._ops[i](x))
            if 0 < value < 1:
                _x.append(value * self._ops[i](x))
        return sum(_x)
    def forwardDART(self,x,weights):
        """
        Args:
            x: input
            weights: weight for each operation
        """
        assert len(self._ops) == len(weights)
        return sum(w*op(x) for w,op in zip(weights,self._ops))
    def forwardGDAS(self,x,weights,index):
        """
        Args:
            x: input
            weights: weight for each operation
            index: index of choosen op
        """
        assert len(self._ops) == len(weights)
        return self._ops[index](x)*weights[index]

class BasicBlock(nn.Module):
    expansion = 1

    def __init__(self, in_planes, planes, stride=1):
        super(BasicBlock, self).__init__()
        self.conv1 = nn.Conv2d(
            in_planes, planes, kernel_size=3, stride=stride, padding=1, bias=False)
        self.bn1 = nn.BatchNorm2d(planes)
        self.conv2 = nn.Conv2d(planes, planes, kernel_size=3,
                               stride=1, padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(planes)

        self.shortcut = nn.Sequential()
        if stride != 1 or in_planes != self.expansion*planes:
            self.shortcut = nn.Sequential(
                nn.Conv2d(in_planes, self.expansion*planes,
                          kernel_size=1, stride=stride, bias=False),
                nn.BatchNorm2d(self.expansion*planes)
            )

    def forward(self, x):
        out = F.relu(self.bn1(self.conv1(x)))
        out = self.bn2(self.conv2(out))
        out += self.shortcut(x)
        out = F.relu(out)
        return out


class ResNetBasicblock(nn.Module):
    def __init__(self, inplanes, planes, stride, affine=True):
        super(ResNetBasicblock, self).__init__()
        assert stride == 1 or stride == 2, 'invalid stride {:}'.format(stride)
        self.conv_a = ReLUConvBN(inplanes, planes, 3, stride, 1)
        self.conv_b = ReLUConvBN(planes, planes, 3, 1, 1)
        if stride == 2:
            self.downsample = nn.Sequential(
                nn.AvgPool2d(kernel_size=2, stride=2, padding=0),
                nn.Conv2d(inplanes, planes, kernel_size=1, stride=1, padding=0, bias=False))
        elif inplanes != planes:
            self.downsample = ReLUConvBN(inplanes, planes, 1, 1, 0)
        else:
            self.downsample = None
        self.in_dim = inplanes
        self.out_dim = planes
        self.stride = stride
        self.num_conv = 2

    def extra_repr(self):
        string = '{name}(inC={in_dim}, outC={out_dim}, stride={stride})'.format(
            name=self.__class__.__name__, **self.__dict__)
        return string

    def forward(self, inputs):

        basicblock = self.conv_a(inputs)
        basicblock = self.conv_b(basicblock)

        if self.downsample is not None:
            residual = self.downsample(inputs)
        else:
            residual = inputs
        return residual + basicblock

# the search cell in darts


class DartsCell(nn.Module):
    def __init__(self, n_nodes, C_pp, C_p, C, reduction_p, reduction, basic_op_list):
        """
        Args:
            n_nodes: # of intermediate n_nodes
            C_pp: C_out[k-2]
            C_p : C_out[k-1]
            C   : C_in[k] (current)
            reduction_p: flag for whether the previous cell is reduction cell or not
            reduction: flag for whether the current cell is reduction cell or not
        """
        super().__init__()
        self.reduction = reduction
        self.n_nodes = n_nodes
        self.basic_op_list = basic_op_list

        # If previous cell is reduction cell, current input size does not match with
        # output size of cell[k-2]. So the output[k-2] should be reduced by preprocessing.
        if reduction_p:
            self.preproc0 = FactorizedReduce(C_pp, C, affine=False)
        else:
            self.preproc0 = StdConv(C_pp, C, 1, 1, 0, affine=False)
        self.preproc1 = StdConv(C_p, C, 1, 1, 0, affine=False)

        # generate dag
        self.dag = nn.ModuleList()
        for i in range(self.n_nodes):
            self.dag.append(nn.ModuleList())
            for j in range(2+i):  # include 2 input nodes
                # reduction should be used only for input node
                stride = 2 if reduction and j < 2 else 1
                op = _MixedOp(C, C, stride, self.basic_op_list)
                self.dag[i].append(op)

    def forward(self, s0, s1, sample):
        s0 = self.preproc0(s0)
        s1 = self.preproc1(s1)

        states = [s0, s1]
        w_dag = darts_weight_unpack(sample, self.n_nodes)
        for edges, w_list in zip(self.dag, w_dag):
            s_cur = sum(edges[i](s, w)
                        for i, (s, w) in enumerate(zip(states, w_list)))
            states.append(s_cur)
        s_out = torch.cat(states[2:], 1)
        return s_out
    def forwardGDAS(self,s0,s1,sample,indexs):
        s0 = self.preproc0(s0)
        s1 = self.preproc1(s1)

        states = [s0, s1]
        w_dag = darts_weight_unpack(sample, self.n_nodes)
        i_dag = gdas_indexes_unpack(indexs, self.n_nodes)
        for edges, w_list, i_list  in zip(self.dag, w_dag, i_dag):
            # at thie moment, it should be int type
            s_cur = sum(edges[i].forwardGDAS(s, w, it)
                        for i, (s, w, it) in enumerate(zip(states, w_list, i_list)))
            states.append(s_cur)
        s_out = torch.cat(states[2:], 1)
        return s_out


class DartsCNN(nn.Module):

    def __init__(self, C=16, n_classes=10, n_layers=8, n_nodes=4, basic_op_list=[]):
        super().__init__()
        stem_multiplier = 3
        self.C_in = 3  # 3
        self.C = C  # 16
        self.n_classes = n_classes  # 10
        self.n_layers = n_layers  # 8
        self.n_nodes = n_nodes  # 4
        self.basic_op_list = ['max_pool_3x3', 'avg_pool_3x3', 'skip_connect', 'sep_conv_3x3',
                              'sep_conv_5x5', 'dil_conv_3x3', 'dil_conv_5x5', 'none'] if len(basic_op_list) == 0 else basic_op_list
        self.non_op_idx = get_op_index(self.basic_op_list, NON_PARAMETER_OP)
        self.para_op_idx = get_op_index(self.basic_op_list, PARAMETER_OP)
        C_cur = stem_multiplier * C  # 3 * 16 = 48
        self.stem = nn.Sequential(
            nn.Conv2d(self.C_in, C_cur, 3, 1, 1, bias=False),
            nn.BatchNorm2d(C_cur)
        )
        # for the first cell, stem is used for both s0 and s1
        # [!] C_pp and C_p is output channel size, but C_cur is input channel size.
        C_pp, C_p, C_cur = C_cur, C_cur, C
        # 48   48   16
        self.cells = nn.ModuleList()
        reduction_p = False
        for i in range(n_layers):
            # Reduce featuremap size and double channels in 1/3 and 2/3 layer.
            if i in [n_layers // 3, 2 * n_layers // 3]:
                C_cur *= 2
                reduction = True
            else:
                reduction = False
            cell = DartsCell(n_nodes, C_pp, C_p, C_cur,
                             reduction_p, reduction, self.basic_op_list)
            reduction_p = reduction
            self.cells.append(cell)
            C_cur_out = C_cur * n_nodes
            C_pp, C_p = C_p, C_cur_out
        self.gap = nn.AdaptiveAvgPool2d(1)
        self.linear = nn.Linear(C_p, n_classes)
        # number of edges per cell
        self.num_edges = sum(list(range(2, self.n_nodes + 2)))
        self.num_ops = len(self.basic_op_list)
        # whole edges
        self.all_edges = 2 * self.num_edges
        self.norm_node_index = self._node_index(n_nodes, input_nodes=2, start_index=0)
        self.reduce_node_index = self._node_index(n_nodes, input_nodes=2, start_index=self.num_edges)

    def forward(self, x, sample):
        s0 = s1 = self.stem(x)

        for cell in self.cells:
            weights = sample[self.num_edges:] if cell.reduction else sample[0:self.num_edges]
            s0, s1 = s1, cell(s0, s1, weights)

        out = self.gap(s1)
        out = out.view(out.size(0), -1)  # flatten
        logits = self.linear(out)
        return logits

    def genotype(self, theta):
        Genotype = namedtuple(
            'Genotype', 'normal normal_concat reduce reduce_concat')
        theta_norm = darts_weight_unpack(
            theta[0:self.num_edges], self.n_nodes)
        theta_reduce = darts_weight_unpack(
            theta[self.num_edges:], self.n_nodes)
        gene_normal = parse_from_numpy(
            theta_norm, k=2, basic_op_list=self.basic_op_list)
        gene_reduce = parse_from_numpy(
            theta_reduce, k=2, basic_op_list=self.basic_op_list)
        concat = range(2, 2+self.n_nodes)  # concat all intermediate nodes
        return Genotype(normal=gene_normal, normal_concat=concat,
                        reduce=gene_reduce, reduce_concat=concat)

    def genotype_to_onehot_sample(self, genotype):
        sample = np.zeros([self.all_edges, len(self.basic_op_list)])
        norm_gene = genotype[0]
        reduce_gene = genotype[2]
        num_select = list(range(2, 2+self.n_nodes))
        for j, _gene in enumerate([norm_gene, reduce_gene]):
            for i, node in enumerate(_gene):
                for op in node:
                    op_name = op[0]
                    op_id = op[1]
                    if i == 0:
                        true_id = op_id + j * self.num_edges
                    else:
                        if i == 1:
                            _temp = num_select[0]
                        else:
                            _temp = sum(num_select[0:i])
                        true_id = op_id + _temp + j * self.num_edges
                    sample[true_id, self.basic_op_list.index(op_name)] = 1
        for i in range(self.all_edges):
            if np.sum(sample[i, :]) == 0:
                sample[i, 7] = 1
        return sample

    def _node_index(self, n_nodes, input_nodes=2, start_index=0):
        node_index = []
        start_index = start_index
        end_index = input_nodes + start_index
        for i in range(n_nodes):
            node_index.append(list(range(start_index, end_index)))
            start_index = end_index
            end_index += input_nodes + i + 1
        return node_index


# This module is used for NAS-Bench-201, represents a small search space with a complete DAG
# Codes come from AutoDL-projects??

class NAS201SearchCell(nn.Module):

    def __init__(self, n_nodes, C_in, C_out, stride, basic_op_list):
        super(NAS201SearchCell, self).__init__()
        self.basic_op_list = basic_op_list
        # generate dag
        self.edges = nn.ModuleDict()
        self.in_dim = C_in
        self.out_dim = C_out
        self.n_nodes = n_nodes
        for i in range(1, n_nodes):
            for j in range(i):
                node_str = '{:}<-{:}'.format(i, j)
                if j == 0:
                    self.edges[node_str] = _MixedOp(
                        C_in, C_out, stride, basic_op_list)
                else:
                    self.edges[node_str] = _MixedOp(
                        C_in, C_out, 1, basic_op_list)
        self.edge_keys = sorted(list(self.edges.keys()))
        self.edge2index = {key: i for i, key in enumerate(self.edge_keys)}
        self.num_edges = len(self.edges)

    def forward(self, inputs, sample):
        nodes = [inputs]
        for i in range(1, self.n_nodes):
            inter_nodes = []
            for j in range(i):
                node_str = '{:}<-{:}'.format(i, j)
                weights = sample[self.edge2index[node_str]]
                inter_nodes.append(self.edges[node_str](nodes[j], weights))
            nodes.append(sum(inter_nodes))
        return nodes[-1]


class NASBench201CNN(nn.Module):
    # def __init__(self, C, N, max_nodes, num_classes, search_space, affine=False, track_running_stats=True):
    def __init__(self, C=16, N=5, max_nodes=4, num_classes=10, basic_op_list=[]):
        super(NASBench201CNN, self).__init__()
        self._C = C
        self._layerN = N
        self.max_nodes = max_nodes
        self.basic_op_list = ['none', 'skip_connect', 'nor_conv_1x1',
                              'nor_conv_3x3', 'avg_pool_3x3'] if len(basic_op_list) == 0 else basic_op_list
        self.non_op_idx = get_op_index(self.basic_op_list, NON_PARAMETER_OP)
        self.para_op_idx = get_op_index(self.basic_op_list, PARAMETER_OP)
        self.stem = nn.Sequential(
            nn.Conv2d(3, C, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(C))

        layer_channels = [C] * N + [C * 2] + \
            [C * 2] * N + [C * 4] + [C * 4] * N
        layer_reductions = [False] * N + [True] + \
            [False] * N + [True] + [False] * N

        C_prev, num_edge, edge2index = C, None, None
        self.cells = nn.ModuleList()
        for index, (C_curr, reduction) in enumerate(zip(layer_channels, layer_reductions)):
            if reduction:
                cell = ResNetBasicblock(C_prev, C_curr, 2)
            else:
                cell = NAS201SearchCell(
                    max_nodes, C_prev, C_curr, 1, self.basic_op_list)
                if num_edge is None:
                    num_edge, edge2index = cell.num_edges, cell.edge2index
                else:
                    assert num_edge == cell.num_edges and edge2index == cell.edge2index, 'invalid {:} vs. {:}.'.format(
                        num_edge, cell.num_edges)
            self.cells.append(cell)
            C_prev = cell.out_dim
        self._Layer = len(self.cells)
        self.edge2index = edge2index
        self.num_edges = num_edge
        self.all_edges = self.num_edges
        self.num_ops = len(self.basic_op_list)
        self.lastact = nn.Sequential(
            nn.BatchNorm2d(C_prev), nn.ReLU(inplace=True))
        self.global_pooling = nn.AdaptiveAvgPool2d(1)
        self.classifier = nn.Linear(C_prev, num_classes)

    def genotype(self, theta):
        genotypes = ''
        for i in range(1, self.max_nodes):
            sub_geno = '|'
            for j in range(i):
                node_str = '{:}<-{:}'.format(i, j)
                weights = theta[self.edge2index[node_str]]
                op_name = self.basic_op_list[np.argmax(weights)]
                sub_geno += '{0}~{1}|'.format(op_name, str(j))
            if i == 1:
                genotypes += sub_geno
            else:
                genotypes += '+' + sub_geno
        return genotypes

    def forward(self, inputs, weight):

        feature = self.stem(inputs)
        for i, cell in enumerate(self.cells):
            if isinstance(cell, ResNetBasicblock):
                feature = cell(feature)
            else:
                feature = cell(feature, weight)
        out = self.lastact(feature)
        out = self.global_pooling(out)
        out = out.view(out.size(0), -1)
        logits = self.classifier(out)
        return logits

# This module is used for GDAS-V1, its cell DAG is based on DARTS
# GDAS(FRC) fixed reduction cell
class GDASReductionCell(nn.Module):
    def __init__(self,C_pp, C_p, C, reduction_p, affine, track_running_stats=True):
        super(GDASReductionCell, self).__init__()
        if reduction_p:
            self.preproc0 = FactorizedReduce(C_pp, C, affine)
        else:
            self.preproc0 = ReLUConvBN(C_pp, C, 1, 1, 0, affine)
        self.preproc1 = ReLUConvBN(C_p, C, 1, 1, 0, affine)

        self.reduction = True
        self.op1 = nn.ModuleList(
                    [nn.Sequential(
                        nn.ReLU(inplace=False),
                        nn.Conv2d(C, C, (1, 3), stride=(1, 2), padding=(0, 1), groups=8, bias=not affine),
                        nn.Conv2d(C, C, (3, 1), stride=(2, 1), padding=(1, 0), groups=8, bias=not affine),
                        nn.BatchNorm2d(C, affine=affine, track_running_stats=track_running_stats),
                        nn.ReLU(inplace=False),
                        nn.Conv2d(C, C, 1, stride=1, padding=0, bias=not affine),
                        nn.BatchNorm2d(C, affine=affine, track_running_stats=track_running_stats)),
                    nn.Sequential(
                        nn.ReLU(inplace=False),
                        nn.Conv2d(C, C, (1, 3), stride=(1, 2), padding=(0, 1), groups=8, bias=not affine),
                        nn.Conv2d(C, C, (3, 1), stride=(2, 1), padding=(1, 0), groups=8, bias=not affine),
                        nn.BatchNorm2d(C, affine=affine, track_running_stats=track_running_stats),
                        nn.ReLU(inplace=False),
                        nn.Conv2d(C, C, 1, stride=1, padding=0, bias=not affine),
                        nn.BatchNorm2d(C, affine=affine, track_running_stats=track_running_stats))])

        self.op2 = nn.ModuleList(
                    [nn.Sequential(
                        nn.MaxPool2d(3, stride=2, padding=1),
                        nn.BatchNorm2d(C, affine=affine, track_running_stats=track_running_stats)),
                    nn.Sequential(
                        nn.MaxPool2d(3, stride=2, padding=1),
                        nn.BatchNorm2d(C, affine=affine, track_running_stats=track_running_stats))])
    @property
    def forward(self,s0,s1, drop_prob = -1):
        s0 = self.preproc0(s0)
        s1 = self.preproc1(s1)

        X0 = self.op1[0] (s0)
        X1 = self.op1[1] (s1)
        if drop_prob > 0.:
            X0, X1 = drop_path_(X0, drop_prob,self.training), drop_path_(X1, drop_prob,self.training)

        #X2 = self.ops2[0] (X0+X1)
        X2 = self.op2[0] (s0)
        X3 = self.op2[1] (s1)
        if drop_prob > 0.:
            X2, X3 = drop_path_(X2, drop_prob,self.training), drop_path_(X3, drop_prob,self.training)
        return torch.cat([X0, X1, X2, X3], dim=1)
class GDAS(DartsCNN):
    def __init__(self, C=16, n_classes=10, n_layers=8, n_nodes=4, basic_op_list=[], tau=10):
        super().__init__(C=16, n_classes=10, n_layers=8, n_nodes=4, basic_op_list=[])
        self.tau=tau

    def set_tau(self, tau):
        self.tau = tau

    def forwardGDAS(self,x,sample,select_i_type='max'):
        def get_gumbel_prob(xins):
            while True:
                gumbels = -torch.empty_like(xins).exponential_().log()
                logits  = (xins.log_softmax(dim=1) + gumbels) / self.tau
                probs   = nn.functional.softmax(logits, dim=1)
                index   = probs.max(-1, keepdim=True)[1]
                one_h   = torch.zeros_like(logits).scatter_(-1, index, 1.0)
                hardwts = one_h - probs.detach() + probs
                if (torch.isinf(gumbels).any()) or (torch.isinf(probs).any()) or (torch.isnan(probs).any()):
                    print("Warning, stuck in gumbel prob")
                    continue
                else: break
            return hardwts, index

        n_weight,n_index=get_gumbel_prob(sample[0:self.num_edges])
        r_weight,r_index=get_gumbel_prob(sample[self.num_edges: ])
        s0=s1=self.stem(x)
        for i , cell in enumerate(self.cells):
            if cell.reduction: weight, index = r_weight, r_index 
            else :             weight, index = n_weight, n_index
            s0, s1 = s1, cell.forwardGDAS(s0,s1,weight,index)
        out=self.gap(s1)
        out=out.view(out.size(0),-1)
        logits=self.linear(out)
        return logits
class GDASFRC(nn.Module):
    def __init__(self):
        pass
    pass
# build API

def _DartsCNN():
    from xnas.core.config import cfg
    return DartsCNN(
        C=cfg.SPACE.CHANNEL,
        n_classes=cfg.SPACE.NUM_CLASSES,
        n_layers=cfg.SPACE.LAYERS,
        n_nodes=cfg.SPACE.NODES,
        basic_op_list=cfg.SPACE.BASIC_OP)
def _GDAS():
    from xnas.core.config import cfg
    return GDAS(
        C=cfg.SPACE.CHANNEL,
        n_classes=cfg.SPACE.NUM_CLASSES,
        n_layers=cfg.SPACE.LAYERS,
        n_nodes=cfg.SPACE.NODES,
        basic_op_list=cfg.SPACE.BASIC_OP,
        tau= cfg.SPACE.tau)
def _GDASFRC():
    from xnas.core.config import cfg
    return GDASFRC(

    )

def _NASbench201():
    from xnas.core.config import cfg
    return NASBench201CNN(C=cfg.SPACE.CHANNEL,
                          N=cfg.SPACE.LAYERS,
                          max_nodes=cfg.SPACE.NODES,
                          num_classes=cfg.SPACE.NUM_CLASSES,
                          basic_op_list=cfg.SPACE.BASIC_OP)
