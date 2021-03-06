import os
import sys
import numpy as np
import torch
import torch.nn.functional as F

sys.path.append('.')
from xnas.core.timer import Timer
from xnas.search_space.cell_based import GDAS

def basic_darts_cnn_test():
    # dartscnn test
    time_ = Timer()
    print("Testing GDAS CNN")
    search_net = GDAS().cuda()
    _random_architecture_weight = torch.randn(
        [search_net.num_edges * 2, len(search_net.basic_op_list)]).cuda()
    _input = torch.randn([4, 3, 32, 32]).cuda()
    time_.tic()
    _out_put = search_net.forwardGDAS(_input, _random_architecture_weight)
    time_.toc()
    print(_out_put.shape)
    print(time_.average_time)
    time_.reset()
    _random_one_hot = torch.Tensor(np.eye(len(search_net.basic_op_list))[
                                   np.random.choice(len(search_net.basic_op_list), search_net.num_edges * 2)]).cuda()
    _input = torch.randn([2, 3, 32, 32]).cuda()
    time_.tic()
    _out_put = search_net.forwardGDAS(_input, _random_one_hot)
    time_.toc()
    print(_out_put.shape)
    print(time_.average_time)

if __name__ == "__main__":
    basic_darts_cnn_test()
    pass
