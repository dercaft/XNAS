import gc
import os
import sys

from torch.utils.tensorboard import SummaryWriter

sys.path.append('.')
import xnas.core.checkpoint as checkpoint
import xnas.core.config as config
import xnas.core.logging as logging
import xnas.core.meters as meters
from xnas.core.builders import build_space
from xnas.core.config import cfg
from xnas.core.trainer import setup_env, test_epoch
from xnas.datasets.loader import _construct_loader
from xnas.search_algorithm.darts import *

def train_one_epoch():
    pass
def valid_one_epoch():
    pass
def main():
    pass
if __name__=="__main__":
    main()