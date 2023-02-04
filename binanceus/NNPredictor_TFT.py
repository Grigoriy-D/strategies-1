# Neural Network Binary Classifier: this subclass uses Google's Temporal Fusion Transformer (TFT) model


import numpy as np
from pandas import DataFrame, Series
import pandas as pd

pd.options.mode.chained_assignment = None  # default='warn'

# Strategy specific imports, files must reside in same folder as strategy
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent))

import logging
import warnings

log = logging.getLogger(__name__)
# log.setLevel(logging.DEBUG)
warnings.simplefilter(action='ignore', category=pd.errors.PerformanceWarning)

import random

import os

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '1'
os.environ['TF_DETERMINISTIC_OPS'] = '1'

import tensorflow as tf
tf.config.set_visible_devices([], 'GPU')

seed = 42
os.environ['PYTHONHASHSEED'] = str(seed)
random.seed(seed)
tf.random.set_seed(seed)
np.random.seed(seed)

from ClassifierDarts import ClassifierDarts
from darts.models import TFTModel


class NNPredictor_TFT(ClassifierDarts):
    is_trained = False
    clean_data_required = False  # training data can contain anomalies
    model_per_pair = False  # separate model per pair

    # override the build_model function in subclasses
    def create_model(self, seq_len, num_features):

        model = TFTModel(input_chunk_length=seq_len,
                         output_chunk_length=self.lookahead,
                         add_relative_index=True,
                         pl_trainer_kwargs=self.get_trainer_args()
                         )
        return model

        return model

    # class-specific load
    def load_from_file(self, model_path, use_gpu=True):
        return TFTModel.load(model_path)