import operator

import numpy as np
from enum import Enum

import pywt
import talib.abstract as ta
from scipy.ndimage import gaussian_filter1d
from sklearn.preprocessing import RobustScaler

import freqtrade.vendor.qtpylib.indicators as qtpylib
import arrow

from freqtrade.exchange import timeframe_to_minutes
from freqtrade.strategy import (IStrategy, merge_informative_pair, stoploss_from_open,
                                IntParameter, DecimalParameter, CategoricalParameter)

from typing import Dict, List, Optional, Tuple, Union
from pandas import DataFrame, Series
from functools import reduce
from datetime import datetime, timedelta
from freqtrade.persistence import Trade

# Get rid of pandas warnings during backtesting
import pandas as pd
import pandas_ta as pta

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

import custom_indicators as cta
from finta import TA as fta

import keras
from keras import layers
from tqdm import tqdm
import time

"""
####################################################################################
LSTM - uses a Long-Short Term Memory neural network to try and predict the future stock price
      
      This works by creating a LSTM model that we train on the historical data, then use that model to predict 
      future values
      
      Note that this is very slow because we are training and running a neural network. 
      This strategy is likely not viable on a configuration of more than a few pairs, and even then needs
      a fast computer, preferably with a GPU
      
      In addition to the normal freqtrade packages, these strategies also require the installation of:
        finta
        keras
        tensorflow

####################################################################################
"""

class LSTM2(IStrategy):


    plot_config = {
        'main_plot': {
            'close': {'color': 'green'},
            'predict': {'color': 'lightpink'},
        },
        'subplots': {
            "Diff": {
                'predict_diff': {'color': 'blue'},
            },
        }
    }


    # Do *not* hyperopt for the roi and stoploss spaces (unless you turn off custom stoploss)

    # ROI table:
    minimal_roi = {
        "0": 0.15,
        "65": 0.075,
        "87": 0.039,
        "305": 0
    }

    # Stoploss:
    stoploss = -0.10

    # Trailing stop:
    trailing_stop = False
    trailing_stop_positive = None
    trailing_stop_positive_offset = 0.0
    trailing_only_offset_is_reached = False

    timeframe = '5m'

    inf_timeframe = '5m'

    use_custom_stoploss = True

    # Recommended
    use_entry_signal = True
    entry_profit_only = False
    ignore_roi_if_entry_signal = True

    # Required
    startup_candle_count: int = 128  # must be power of 2
    process_only_new_candles = False

    # Strategy-specific global vars

    inf_mins = timeframe_to_minutes(inf_timeframe)
    data_mins = timeframe_to_minutes(timeframe)
    inf_ratio = int(inf_mins / data_mins)

    # These parameters control much of the behaviour because they control the generation of the training data
    # Unfortunately, these cannot be hyperopt params because they are used in populate_indicators, which is only run
    # once during hyperopt
    # lookahead_hours = 1.0
    lookahead_hours = 1.0
    n_profit_stddevs = 0.0
    n_loss_stddevs = 0.0
    min_f1_score = 0.70

    curr_lookahead = int(12 * lookahead_hours)

    curr_pair = ""
    custom_trade_info = {}

    # profit/loss thresholds used for assessing entry/exit signals. Keep these realistic!
    # Note: if self.dynamic_gain_thresholds is True, these will be adjusted for each pair, based on historical mean
    default_profit_threshold = 0.3
    default_loss_threshold = -0.3
    profit_threshold = default_profit_threshold
    loss_threshold = default_loss_threshold
    dynamic_gain_thresholds = True  # dynamically adjust gain thresholds based on actual mean (beware, training data could be bad)


    num_pairs = 0
    pair_model_info = {}  # holds model-related info for each pair
    curr_dataframe:DataFrame = None
    normalise_data = True

    # debug flags
    first_time = True  # mostly for debug
    first_run = True  # used to identify first time through entry/exit populate funcs

    dbg_verbose = True  # controls debug output
    dbg_curr_df: DataFrame = None  # for debugging of current dataframe

    # variables to track state
    class State(Enum):
        INIT = 1
        POPULATE = 2
        STOPLOSS = 3
        RUNNING = 4

    ###################################

    # Strategy Specific Variable Storage

    ## Hyperopt Variables

    # LSTM hyperparams

    # Custom exit Profit (formerly Dynamic ROI)

    exit_params = {
        "pHSL": -0.186,
        "pPF_1": 0.011,
        "pPF_2": 0.071,
        "pSL_1": 0.02,
        "pSL_2": 0.063
    }

    # hard stoploss profit
    pHSL = DecimalParameter(-0.200, -0.040, default=-0.08, decimals=3, space='sell', load=True)
    # profit threshold 1, trigger point, SL_1 is used
    pPF_1 = DecimalParameter(0.008, 0.020, default=0.016, decimals=3, space='sell', load=True)
    pSL_1 = DecimalParameter(0.008, 0.020, default=0.011, decimals=3, space='sell', load=True)

    # profit threshold 2, SL_2 is used
    pPF_2 = DecimalParameter(0.040, 0.100, default=0.080, decimals=3, space='sell', load=True)
    pSL_2 = DecimalParameter(0.020, 0.070, default=0.040, decimals=3, space='sell', load=True)


    ################################


    """
    Informative Pair Definitions
    """

    def inf_pairs(self):
        # # all pairs in the whitelist are also in the informative list
        # pairs = self.dp.current_whitelist()
        # inf_pairs = [(pair, self.inf_timeframe) for pair in pairs]
        # return inf_pairs
        return []

    ###################################

    """
    Indicator Definitions
    """

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:

        # TODO: add in classifiers from statsmodels
        # TODO; do grid search for MLP hyperparams
        # TODO: keeps stats on classifier performance and selection

        # Base pair inf timeframe indicators
        curr_pair = metadata['pair']
        self.curr_pair = curr_pair
        self.curr_dataframe = dataframe

        self.curr_lookahead = int(12 * self.lookahead_hours)
        self.dbg_curr_df = dataframe

        # reset profit/loss thresholds
        self.profit_threshold = self.default_profit_threshold
        self.loss_threshold = self.default_loss_threshold

        if LSTM2.first_time:
            LSTM2.first_time = False
            print("")
            print("***************************************")
            print("** Warning: startup can be very slow **")
            print("***************************************")

            print("    Lookahead: ", self.curr_lookahead, " candles (", self.lookahead_hours, " hours)")
            print("    Thresholds - Profit:{:.2f}% Loss:{:.2f}%".format(self.profit_threshold,
                                                                        self.loss_threshold))

        print("")
        print(curr_pair)

        # populate the training indicators
        dataframe = self.add_training_indicators(dataframe)

        # train the model
        if self.dbg_verbose:
            print("    training model...")

        dataframe = self.train_model(dataframe, curr_pair)

        # add predictions
        if self.dbg_verbose:
            print("    running predictions...")

        dataframe = self.add_predictions(dataframe, curr_pair)

        # Custom Stoploss
        if self.dbg_verbose:
            print("    updating stoploss data...")
        dataframe = self.add_indicators(dataframe)
        dataframe = self.add_stoploss_indicators(dataframe, curr_pair)

        return dataframe

    ###################################

    # add in any indicators to be used for training
    def add_training_indicators(self, dataframe: DataFrame) -> DataFrame:

        # don't add too many indicators, it just muddles the prediction

        # price, high, low, volume automatically included

        win_size = max(self.curr_lookahead, 14)

        dataframe['tema'] = ta.TEMA(dataframe, timeperiod=win_size)
        dataframe['rsi'] = ta.RSI(dataframe, timeperiod=win_size)

        # MFI
        dataframe['mfi'] = ta.MFI(dataframe)

        # ATR
        dataframe['atr'] = ta.ATR(dataframe, timeperiod=win_size)

        # Stoch fast
        stoch_fast = ta.STOCHF(dataframe)
        dataframe['fastd'] = stoch_fast['fastd']
        dataframe['fastk'] = stoch_fast['fastk']
        dataframe['fast_diff'] = dataframe['fastd'] - dataframe['fastk']

        # ADX
        dataframe['adx'] = ta.ADX(dataframe)

        # Plus Directional Indicator / Movement
        dataframe['dm_plus'] = ta.PLUS_DM(dataframe)
        dataframe['di_plus'] = ta.PLUS_DI(dataframe)

        # Minus Directional Indicator / Movement
        dataframe['dm_minus'] = ta.MINUS_DM(dataframe)
        dataframe['di_minus'] = ta.MINUS_DI(dataframe)
        dataframe['dm_delta'] = dataframe['dm_plus'] - dataframe['dm_minus']
        dataframe['di_delta'] = dataframe['di_plus'] - dataframe['di_minus']

        # gain relative to previous candle
        dataframe['gain'] = (dataframe['close'] - dataframe['close'].shift(1)) / dataframe['close'].shift(1)

        # longer term high/low
        dataframe['low_trend'] = dataframe['close'].rolling(window=self.startup_candle_count).min()
        dataframe['high_trend'] = dataframe['close'].rolling(window=self.startup_candle_count).max()

        return dataframe


    # populate dataframe with desired technical indicators
    # NOTE: OK to throw (almost) anything in here, just add it to the parameter list
    # The whole idea is to create a dimension-reduced mapping anyway
    # Warning: do not use indicators that might produce 'inf' results, it messes up the scaling
    def add_indicators(self, dataframe: DataFrame) -> DataFrame:

        win_size = max(self.curr_lookahead, 14)

        # these averages are used internally, do not remove!
        dataframe['sma'] = ta.SMA(dataframe, timeperiod=win_size)
        dataframe['ema'] = ta.EMA(dataframe, timeperiod=win_size)
        # dataframe['tema'] = ta.TEMA(dataframe, timeperiod=win_size)
        dataframe['tema_stddev'] = dataframe['tema'].rolling(win_size).std()
        # dataframe['rsi'] = ta.RSI(dataframe, timeperiod=win_size)

        # these are here for reference. Uncomment anything you want to use
 
        # # MACD
        # macd = ta.MACD(dataframe)
        # dataframe['macd'] = macd['macd']
        # dataframe['macdsignal'] = macd['macdsignal']
        # dataframe['macdhist'] = macd['macdhist']
        #
        # # Bollinger Bands (must include these)
        # bollinger = qtpylib.bollinger_bands(dataframe['close'], window=20, stds=2)
        # dataframe['bb_lowerband'] = bollinger['lower']
        # dataframe['bb_middleband'] = bollinger['mid']
        # dataframe['bb_upperband'] = bollinger['upper']
        # dataframe['bb_width'] = ((dataframe['bb_upperband'] - dataframe['bb_lowerband']) / dataframe['bb_middleband'])
        # dataframe["bb_gain"] = ((dataframe["bb_upperband"] - dataframe["close"]) / dataframe["close"])
        # dataframe["bb_loss"] = ((dataframe["bb_lowerband"] - dataframe["close"]) / dataframe["close"])
        #
        # # Donchian Channels
        # dataframe['dc_upper'] = ta.MAX(dataframe['high'], timeperiod=win_size)
        # dataframe['dc_lower'] = ta.MIN(dataframe['low'], timeperiod=win_size)
        # dataframe['dc_mid'] = ta.TEMA(((dataframe['dc_upper'] + dataframe['dc_lower']) / 2), timeperiod=win_size)
        #
        # dataframe["dcbb_dist_upper"] = (dataframe["dc_upper"] - dataframe['bb_upperband'])
        # dataframe["dcbb_dist_lower"] = (dataframe["dc_lower"] - dataframe['bb_lowerband'])
        #
        # # Fibonacci Levels (of Donchian Channel)
        # dataframe['dc_dist'] = (dataframe['dc_upper'] - dataframe['dc_lower'])
        # dataframe['dc_hf'] = dataframe['dc_upper'] - dataframe['dc_dist'] * 0.236  # Highest Fib
        # dataframe['dc_chf'] = dataframe['dc_upper'] - dataframe['dc_dist'] * 0.382  # Centre High Fib
        # dataframe['dc_clf'] = dataframe['dc_upper'] - dataframe['dc_dist'] * 0.618  # Centre Low Fib
        # dataframe['dc_lf'] = dataframe['dc_upper'] - dataframe['dc_dist'] * 0.764  # Low Fib
        #
        #  # Keltner Channels
        # keltner = qtpylib.keltner_channel(dataframe)
        # dataframe["kc_upper"] = keltner["upper"]
        # dataframe["kc_lower"] = keltner["lower"]
        # dataframe["kc_mid"] = keltner["mid"]
        #
        # # Williams %R
        # dataframe['wr'] = 0.02 * (williams_r(dataframe, period=14) + 50.0)
        #
        # # Fisher RSI
        # rsi = 0.1 * (dataframe['rsi'] - 50)
        # dataframe['fisher_rsi'] = (np.exp(2 * rsi) - 1) / (np.exp(2 * rsi) + 1)
        #
        # # Combined Fisher RSI and Williams %R
        # dataframe['fisher_wr'] = (dataframe['wr'] + dataframe['fisher_rsi']) / 2.0
        #
        #
        # # MFI
        # dataframe['mfi'] = ta.MFI(dataframe)
        #
        # # ATR
        # dataframe['atr'] = ta.ATR(dataframe, timeperiod=win_size)
        #
        # # Hilbert Transform Indicator - SineWave
        # hilbert = ta.HT_SINE(dataframe)
        # dataframe['htsine'] = hilbert['sine']
        # dataframe['htleadsine'] = hilbert['leadsine']
        #
        # # ADX
        # dataframe['adx'] = ta.ADX(dataframe)
        #
        # # Plus Directional Indicator / Movement
        # dataframe['dm_plus'] = ta.PLUS_DM(dataframe)
        # dataframe['di_plus'] = ta.PLUS_DI(dataframe)
        #
        # # Minus Directional Indicator / Movement
        # dataframe['dm_minus'] = ta.MINUS_DM(dataframe)
        # dataframe['di_minus'] = ta.MINUS_DI(dataframe)
        # dataframe['dm_delta'] = dataframe['dm_plus'] - dataframe['dm_minus']
        # dataframe['di_delta'] = dataframe['di_plus'] - dataframe['di_minus']

        # # Stoch fast
        # stoch_fast = ta.STOCHF(dataframe)
        # dataframe['fastd'] = stoch_fast['fastd']
        # dataframe['fastk'] = stoch_fast['fastk']
        # dataframe['fast_diff'] = dataframe['fastd'] - dataframe['fastk']
        #
        # # SAR Parabol
        # dataframe['sar'] = ta.SAR(dataframe)
        #
        # dataframe['mom'] = ta.MOM(dataframe, timeperiod=14)
        #
        # # priming indicators
        # dataframe['color'] = np.where((dataframe['close'] > dataframe['open']), 1.0, -1.0)
        # dataframe['rsi_7'] = ta.RSI(dataframe, timeperiod=7)
        # dataframe['roc_6'] = ta.ROC(dataframe, timeperiod=6)
        # dataframe['primed'] = np.where(dataframe['color'].rolling(3).sum() == 3.0, 1.0, -1.0)
        # dataframe['in_the_mood'] = np.where(dataframe['rsi_7'] > dataframe['rsi_7'].rolling(12).mean(), 1.0, -1.0)
        # dataframe['moist'] = np.where(qtpylib.crossed_above(dataframe['macd'], dataframe['macdsignal']), 1.0, -1.0)
        # dataframe['throbbing'] = np.where(dataframe['roc_6'] > dataframe['roc_6'].rolling(12).mean(), 1.0, -1.0)
        #
        # # Oscillators
        #
        # # EWO
        # dataframe['ewo'] = ewo(dataframe, 50, 200)
        #
        # # Ultimate Oscillator
        # dataframe['uo'] = ta.ULTOSC(dataframe)
        #
        # # Aroon, Aroon Oscillator
        # aroon = ta.AROON(dataframe)
        # dataframe['aroonup'] = aroon['aroonup']
        # dataframe['aroondown'] = aroon['aroondown']
        # dataframe['aroonosc'] = ta.AROONOSC(dataframe)
        #
        # # Awesome Oscillator
        # dataframe['ao'] = qtpylib.awesome_oscillator(dataframe)
        #
        # # Commodity Channel Index: values [Oversold:-100, Overbought:100]
        # dataframe['cci'] = ta.CCI(dataframe)

        return dataframe


    def add_stoploss_indicators(self, dataframe: DataFrame, pair) -> DataFrame:
        #
        # if not pair in self.custom_trade_info:
        #     self.custom_trade_info[pair] = {}
        #     if not 'had_trend' in self.custom_trade_info[pair]:
        #         self.custom_trade_info[pair]['had_trend'] = False
        #
        # # Indicators used for ROI and Custom Stoploss
        #
        # # RMI: https://www.tradingview.com/script/kwIt9OgQ-Relative-Momentum-Index/
        # dataframe['rmi'] = cta.RMI(dataframe, length=24, mom=5)
        #
        # # Trends
        # dataframe['candle_up'] = np.where(dataframe['close'] >= dataframe['close'].shift(), 1.0, -1.0)
        # dataframe['candle_up_trend'] = np.where(dataframe['candle_up'].rolling(5).sum() > 0.0, 1.0, -1.0)
        # dataframe['candle_up_seq'] = dataframe['candle_up'].rolling(5).sum()
        #
        # dataframe['candle_dn'] = np.where(dataframe['close'] < dataframe['close'].shift(), 1.0, -1.0)
        # dataframe['candle_dn_trend'] = np.where(dataframe['candle_up'].rolling(5).sum() > 0.0, 1.0, -1.0)
        # dataframe['candle_dn_seq'] = dataframe['candle_up'].rolling(5).sum()
        #
        # dataframe['rmi_up'] = np.where(dataframe['rmi'] >= dataframe['rmi'].shift(), 1.0, -1.0)
        # dataframe['rmi_up_trend'] = np.where(dataframe['rmi_up'].rolling(5).sum() > 0.0, 1.0, -1.0)
        #
        # dataframe['rmi_dn'] = np.where(dataframe['rmi'] <= dataframe['rmi'].shift(), 1.0, -1.0)
        # dataframe['rmi_dn_count'] = dataframe['rmi_dn'].rolling(8).sum()
        #
        # ssldown, sslup = cta.SSLChannels_ATR(dataframe, length=21)
        # dataframe['sroc'] = cta.SROC(dataframe, roclen=21, emalen=13, smooth=21)
        # dataframe['ssl_dir'] = 0
        # dataframe['ssl_dir'] = np.where(sslup > ssldown, 1.0, -1.0)
        #
        # # TODO: remove/fix any columns that contain 'inf'
        # self.check_inf(dataframe)
        #
        # # TODO: fix NaNs
        # dataframe.fillna(0.0, inplace=True)

        return dataframe

    # add columns based on predictions. Do not call until after model has been trained
    def add_predictions(self, dataframe: DataFrame, pair) -> DataFrame:

        win_size = max(self.curr_lookahead, 14)

        dataframe['predict'] = self.batch_predictions(dataframe)
        dataframe['predict_smooth'] = dataframe['predict'].rolling(window=win_size).apply(self.roll_strong_smooth)

        dataframe['predict_diff'] = 100.0 * (dataframe['predict'] - dataframe['close']) / dataframe['close']

        # dataframe['predict_diff'] = 100.0 * (dataframe['predict_smooth'] - dataframe['smooth']) / dataframe['smooth']

        return dataframe

    ################################

    def train_model(self, dataframe: DataFrame, pair) -> DataFrame:

        # if first time through for this pair, add entry to pair_model_info
        if not (pair in self.pair_model_info):
            self.pair_model_info[pair] = { 'model': None }

        if self.pair_model_info[pair]['model'] == None:
            print("    Creating LSTM model for: ", pair)
            self.pair_model_info[pair]['model'] = self.get_lstm(dataframe.shape[1])

        model = self.pair_model_info[pair]['model']

        # set up training and test data

        nepochs = 4

        # get a mormalised version, then extract data
        # df_norm = self.norm_dataframe(dataframe)
        df = dataframe.copy()
        df = df.shift(-self.startup_candle_count) # don't use data from startup period
        df = self.convert_date(df)
        close_col = df.columns.get_loc("close")
        scaler = RobustScaler()
        df_norm = scaler.fit_transform(df)


        # constrain size to what will be available in run modes
        df_size = df_norm.shape[0]
        data_size = int(min(975, df_size))
        train_size = int(0.8 * data_size) - self.curr_lookahead
        test_size = data_size - train_size - self.curr_lookahead

        # take the middle part of the full dataframe
        train_start = int((df_size - (train_size + test_size + self.curr_lookahead)) / 2)
        train_result_start = train_start + self.curr_lookahead
        test_start = train_start + train_size
        test_result_start = test_start + self.curr_lookahead

        # print("dtrain_start:{} train_result_start:{} test_start:{} test_result_start:{} "
        #       .format(train_start, train_result_start, test_start, test_result_start))

        train_df_norm = df_norm[train_start:train_start+train_size, :]
        train_results_norm = df_norm[train_result_start:train_result_start+train_size, close_col]

        # print(train_df_norm[:, close_col])
        # print(train_results_norm)

        test_df_norm = df_norm[test_start:test_start+test_size, :]
        test_results_norm = df_norm[test_result_start:test_result_start+test_size, close_col]

        # re-shape into format expected by LSTM model
        train_df_norm = np.reshape(np.array(train_df_norm), (train_df_norm.shape[0], 1, train_df_norm.shape[1]))
        test_df_norm = np.reshape(np.array(test_df_norm), (test_df_norm.shape[0], 1, test_df_norm.shape[1]))
        train_results_norm = np.array(train_results_norm).reshape(-1, 1)
        test_results_norm = np.array(test_results_norm).reshape(-1, 1)


        print("")
        print("    train_data:", train_df_norm.shape, " train_results:", train_results_norm.shape)
        print("    test_data: ", test_df_norm.shape, " test_results: ", test_results_norm.shape)
        print("")

        # train the model
        print("    fitting model...")
        print("")
        model.fit(train_df_norm, train_results_norm, batch_size=32, epochs=nepochs,
                  validation_data=(test_df_norm, test_results_norm), verbose=1)
        print("")

        # # test the model
        # print("Evaluate on test data...")
        # results = model.evaluate(test_df_norm, test_results_norm, batch_size=32)
        # print("results:", results)

        return dataframe


    def get_lstm(self, nfeatures:int):
        model = keras.Sequential()

        # # complex model:
        # model.add(layers.LSTM(32, return_sequences=True, input_shape=(1, nfeatures)))
        # model.add(layers.Dropout(rate=0.2))
        # model.add(layers.LSTM(32, return_sequences=True))
        # model.add(layers.Dropout(rate=0.2))
        # model.add(layers.LSTM(32, return_sequences=False))
        # model.add(layers.Dropout(rate=0.2))
        # model.add(layers.Dense(8))
        # model.add(layers.Dense(1))

        # simplest possible model:
        model.add(layers.LSTM(32, return_sequences=True, input_shape=(1, nfeatures)))
        model.add(layers.Dense(1, activation='linear'))

        # model.summary()
        model.compile(optimizer='adam',
                      loss='mean_squared_error',
                      metrics=[keras.metrics.MeanAbsoluteError()])
        return model

    ################################

    def get_predictions(self, dataframe):

        # get the model
        model = self.pair_model_info[self.curr_pair]['model']

        # scale/normalise
        # df_norm = self.norm_dataframe(dataframe)
        df = self.convert_date(dataframe)
        close_col = df.columns.get_loc("close")
        scaler = RobustScaler().fit(df)
        df_norm = scaler.transform(df)

        df_norm3 = np.reshape(np.array(df_norm), (df_norm.shape[0], 1, df_norm.shape[1]))

        # # df_norm = np.array(df_norm)
        # df_norm = df_norm.to_numpy()

        # run the prediction
        preds_notrend = model.predict(df_norm3, verbose=0)
        preds_notrend = np.array(preds_notrend[:, 0]).reshape(-1, 1)

        # re-trend
        # predictions = self.denorm_column(preds_notrend, dataframe['close'])

        # slight cheat - replace 'close' column with predictions, then inverse scale
        cl_col = df_norm[:, close_col]

        # n1 = int(len(cl_col)/2)
        # n2 = n1-self.curr_lookahead
        # print('close:  ', cl_col[n1:n1+16])
        # print('predict:', preds_notrend[n2:n2+16, 0])

        df_norm[:, close_col] = preds_notrend[:, 0]
        inv_y = scaler.inverse_transform(df_norm)
        # print("preds_notrend:", preds_notrend.shape, " df_norm:", df_norm.shape, " inv_y:", inv_y.shape)
        predictions = inv_y[:, close_col]

        return predictions

    # run prediction in batches over the entire history
    def batch_predictions(self, dataframe:DataFrame):
        # prediction does not work well when run over a large dataset, so divide into chunks and predict for each one
        # then concatenate the results and return
        predictions: np.array = []
        batch_size = 64
        nruns = int(dataframe.shape[0] / batch_size)
        for i in tqdm(range(nruns), desc="    Predicting…", ascii=True, ncols=75):
            # for i in range(nruns):
            start = i*batch_size
            end = start + batch_size
            # print("start:{} end:{}".format(start, end))
            df = dataframe.iloc[start : end].copy()
            # print(df)
            preds = self.get_predictions(df)
            # print(preds)
            predictions = np.concatenate((predictions, preds))


        size_left = dataframe.shape[0] - (nruns * batch_size)
        if size_left > 0:
            df = dataframe.iloc[-size_left:]
            preds = self.get_predictions(df)
            predictions = np.concatenate((predictions, preds))

        print("runs:{} predictions:{}".format(nruns, len(predictions)))
        return predictions

    def roll_get_prediction(self, col) -> np.float:
        # must return scalar, so just calculate prediction and take last value

        # print(col.index)
        # get reference to slice of dataframe using the index of the supplied column
        df = self.curr_dataframe.loc[col.index]

        predictions = self.get_predictions(df)

        length = len(predictions)
        if length > 0:
            return predictions[length - 1]
        else:
            # cannot calculate (e.g. at startup), just return original value
            return col[len(col)-1]


    # returns (rolling) smoothed version of input column
    def roll_smooth(self, col) -> np.float:
        # must return scalar, so just calculate prediction and take last value

        smooth = gaussian_filter1d(col, 4)
        # smooth = gaussian_filter1d(col, 2)

        length = len(smooth)
        if length > 0:
            return smooth[length - 1]
        else:
            return col[len(col) - 1]

    def roll_strong_smooth(self, col) -> np.float:
        # must return scalar, so just calculate prediction and take last value

        smooth = gaussian_filter1d(col, 24)

        length = len(smooth)
        if length > 0:
            return smooth[length - 1]
        else:
            return col[len(col) - 1]


    ################################

    clip_outliers = False

    # convert date column to number
    def convert_date(self, dataframe: DataFrame) -> DataFrame:
        df = dataframe.copy()
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date']).astype('int64')
            df.set_index('date')
            df.reindex()
        return df

    # Normalise a dataframe
    def norm_dataframe(self, dataframe: DataFrame) -> DataFrame:
        self.check_inf(dataframe)

        df = self.convert_date(dataframe)

        df = ((df - df.mean()) / df.std())
        if self.clip_outliers:
            df = df.clip(lower=-3.0, upper=3.0)
        return df

    # normalises a column. Data is in units of 1 stddev, i.e. a value of 1.0 represents 1 stdev above mean
    def norm_column(self, col):
        a = np.array(col)
        norm = (a - a.mean()) / a.std()
        if self.clip_outliers:
            norm = norm.clip(min=-3.0, max=3.0)
        return norm

    # denormalise a column, based on the supplied (not normalised) reference column
    def denorm_column(self, col, refcol):
        a = np.array(refcol)
        s = a.std()
        if self.clip_outliers:
            s = s.clip(min=-3.0, max=3.0)
        m = a.mean()
        return (col * s) + m

    def check_inf(self, dataframe: DataFrame):
        col_name = dataframe.columns.to_series()[np.isinf(dataframe).any()]
        if len(col_name) > 0:
            print("***")
            print("*** Infinity in cols: ", col_name)
            print("***")

    ################################

    """
    entry Signal
    """

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = []
        dataframe.loc[:, 'enter_tag'] = ''
        curr_pair = metadata['pair']

        conditions.append(dataframe['volume'] > 0)

        # add some fairly loose guards, to help prevent 'bad' predictions

        # # ATR in entry range
        # conditions.append(dataframe['atr_signal'] > 0.0)

        # some trading volume
        conditions.append(dataframe['volume'] > 0)

        # # Fisher RSI + Williams combo
        # conditions.append(dataframe['fisher_wr'] < -0.7)
        #
        # # below Bollinger mid-point
        # conditions.append(dataframe['close'] < dataframe['bb_middleband'])

        # LSTM/Classifier triggers
        lstm_cond = (
            (qtpylib.crossed_above(dataframe['predict_diff'], 2.0))
        )
        conditions.append(lstm_cond)

        # set entry tags
        dataframe.loc[lstm_cond, 'enter_tag'] += 'lstm_entry '

        if conditions:
            dataframe.loc[reduce(lambda x, y: x & y, conditions), 'enter'] = 1
        else:
            dataframe['enter'] = 0

        return dataframe

    ###################################

    """
    exit Signal
    """

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = []
        dataframe.loc[:, 'exit_tag'] = ''
        curr_pair = metadata['pair']

        conditions.append(dataframe['volume'] > 0)

        # # ATR in exit range
        # conditions.append(dataframe['atr_signal'] <= 0.0)

        # # above Bollinger mid-point
        # conditions.append(dataframe['close'] > dataframe['bb_middleband'])

        # # Fisher RSI + Williams combo
        # conditions.append(dataframe['fisher_wr'] > 0.5)

        # LSTM triggers
        lstm_cond = (
            qtpylib.crossed_below(dataframe['predict_diff'], -2.0)
        )

        conditions.append(lstm_cond)

        dataframe.loc[lstm_cond, 'exit_tag'] += 'lstm_exit '

        if conditions:
            dataframe.loc[reduce(lambda x, y: x & y, conditions), 'exit'] = 1
        else:
            dataframe['exit'] = 0

        return dataframe

    ###################################

    """
    Custom Stoploss
    """

    def custom_stoploss(self, pair: str, trade: 'Trade', current_time: datetime, current_rate: float,
                        current_profit: float, **kwargs) -> float:

        # hard stoploss profit
        HSL = self.pHSL.value
        PF_1 = self.pPF_1.value
        SL_1 = self.pSL_1.value
        PF_2 = self.pPF_2.value
        SL_2 = self.pSL_2.value

        # For profits between PF_1 and PF_2 the stoploss (sl_profit) used is linearly interpolated
        # between the values of SL_1 and SL_2. For all profits above PL_2 the sl_profit value
        # rises linearly with current profit, for profits below PF_1 the hard stoploss profit is used.

        if (current_profit > PF_2):
            sl_profit = SL_2 + (current_profit - PF_2)
        elif (current_profit > PF_1):
            sl_profit = SL_1 + ((current_profit - PF_1) * (SL_2 - SL_1) / (PF_2 - PF_1))
        else:
            sl_profit = HSL

        # Only for hyperopt invalid return
        if (sl_profit >= current_profit):
            return -0.99

        return min(-0.01, max(stoploss_from_open(sl_profit, current_profit), -0.99))

    ###################################
    #
    # """
    # Custom exit
    # """
    #
    # def custom_exit(self, pair: str, trade: 'Trade', current_time: 'datetime', current_rate: float,
    #                 current_profit: float, **kwargs):
    #
    #     dataframe, _ = self.dp.get_analyzed_dataframe(pair=pair, timeframe=self.timeframe)
    #     last_candle = dataframe.iloc[-1].squeeze()
    #
    #     trade_dur = int((current_time.timestamp() - trade.open_date_utc.timestamp()) // 60)
    #     max_profit = max(0, trade.calc_profit_ratio(trade.max_rate))
    #     pullback_value = max(0, (max_profit - self.cexit_pullback_amount.value))
    #     in_trend = False
    #
    #     # Determine our current ROI point based on the defined type
    #     if self.cexit_roi_type.value == 'static':
    #         min_roi = self.cexit_roi_start.value
    #     elif self.cexit_roi_type.value == 'decay':
    #         min_roi = cta.linear_decay(self.cexit_roi_start.value, self.cexit_roi_end.value, 0,
    #                                    self.cexit_roi_time.value, trade_dur)
    #     elif self.cexit_roi_type.value == 'step':
    #         if trade_dur < self.cexit_roi_time.value:
    #             min_roi = self.cexit_roi_start.value
    #         else:
    #             min_roi = self.cexit_roi_end.value
    #
    #     # Determine if there is a trend
    #     if self.cexit_trend_type.value == 'rmi' or self.cexit_trend_type.value == 'any':
    #         if last_candle['rmi_up_trend'] == 1:
    #             in_trend = True
    #     if self.cexit_trend_type.value == 'ssl' or self.cexit_trend_type.value == 'any':
    #         if last_candle['ssl_dir'] == 1:
    #             in_trend = True
    #     if self.cexit_trend_type.value == 'candle' or self.cexit_trend_type.value == 'any':
    #         if last_candle['candle_up_trend'] == 1:
    #             in_trend = True
    #
    #     # Don't exit if we are in a trend unless the pullback threshold is met
    #     if in_trend == True and current_profit > 0:
    #         # Record that we were in a trend for this trade/pair for a more useful exit message later
    #         self.custom_trade_info[trade.pair]['had_trend'] = True
    #         # If pullback is enabled and profit has pulled back allow a sell, maybe
    #         if self.cexit_pullback.value == True and (current_profit <= pullback_value):
    #             if self.cexit_pullback_respect_roi.value == True and current_profit > min_roi:
    #                 return 'intrend_pullback_roi'
    #             elif self.cexit_pullback_respect_roi.value == False:
    #                 if current_profit > min_roi:
    #                     return 'intrend_pullback_roi'
    #                 else:
    #                     return 'intrend_pullback_noroi'
    #         # We are in a trend and pullback is disabled or has not happened or various criteria were not met, hold
    #         return None
    #     # If we are not in a trend, just use the roi value
    #     elif in_trend == False:
    #         if self.custom_trade_info[trade.pair]['had_trend']:
    #             if current_profit > min_roi:
    #                 self.custom_trade_info[trade.pair]['had_trend'] = False
    #                 return 'trend_roi'
    #             elif self.cexit_endtrend_respect_roi.value == False:
    #                 self.custom_trade_info[trade.pair]['had_trend'] = False
    #                 return 'trend_noroi'
    #         elif current_profit > min_roi:
    #             return 'notrend_roi'
    #     else:
    #         return None


#######################

# Utility functions


# Elliot Wave Oscillator
def ewo(dataframe, sma1_length=5, sma2_length=35):
    sma1 = ta.EMA(dataframe, timeperiod=sma1_length)
    sma2 = ta.EMA(dataframe, timeperiod=sma2_length)
    smadif = (sma1 - sma2) / dataframe['close'] * 100
    return smadif


# Chaikin Money Flow
def chaikin_money_flow(dataframe, n=20, fillna=False) -> Series:
    """Chaikin Money Flow (CMF)
    It measures the amount of Money Flow Volume over a specific period.
    http://stockcharts.com/school/doku.php?id=chart_school:technical_indicators:chaikin_money_flow_cmf
    Args:
        dataframe(pandas.Dataframe): dataframe containing ohlcv
        n(int): n period.
        fillna(bool): if True, fill nan values.
    Returns:
        pandas.Series: New feature generated.
    """
    mfv = ((dataframe['close'] - dataframe['low']) - (dataframe['high'] - dataframe['close'])) / (
            dataframe['high'] - dataframe['low'])
    mfv = mfv.fillna(0.0)  # float division by zero
    mfv *= dataframe['volume']
    cmf = (mfv.rolling(n, min_periods=0).sum()
           / dataframe['volume'].rolling(n, min_periods=0).sum())
    if fillna:
        cmf = cmf.replace([np.inf, -np.inf], np.nan).fillna(0)
    return Series(cmf, name='cmf')


# Williams %R
def williams_r(dataframe: DataFrame, period: int = 14) -> Series:
    """Williams %R, or just %R, is a technical analysis oscillator showing the current closing price in relation to the high and low
        of the past N days (for a given N). It was developed by a publisher and promoter of trading materials, Larry Williams.
        Its purpose is to tell whether a stock or commodity market is trading near the high or the low, or somewhere in between,
        of its recent trading range.
        The oscillator is on a negative scale, from −100 (lowest) up to 0 (highest).
    """

    highest_high = dataframe["high"].rolling(center=False, window=period).max()
    lowest_low = dataframe["low"].rolling(center=False, window=period).min()

    WR = Series(
        (highest_high - dataframe["close"]) / (highest_high - lowest_low),
        name=f"{period} Williams %R",
    )

    return WR * -100


# Volume Weighted Moving Average
def vwma(dataframe: DataFrame, length: int = 10):
    """Indicator: Volume Weighted Moving Average (VWMA)"""
    # Calculate Result
    pv = dataframe['close'] * dataframe['volume']
    vwma = Series(ta.SMA(pv, timeperiod=length) / ta.SMA(dataframe['volume'], timeperiod=length))
    vwma = vwma.fillna(0, inplace=True)
    return vwma


# Exponential moving average of a volume weighted simple moving average
def ema_vwma_osc(dataframe, len_slow_ma):
    slow_ema = Series(ta.EMA(vwma(dataframe, len_slow_ma), len_slow_ma))
    return ((slow_ema - slow_ema.shift(1)) / slow_ema.shift(1)) * 100


def t3_average(dataframe, length=5):
    """
    T3 Average by HPotter on Tradingview
    https://www.tradingview.com/script/qzoC9H1I-T3-Average/
    """
    df = dataframe.copy()

    df['xe1'] = ta.EMA(df['close'], timeperiod=length)
    df['xe1'].fillna(0, inplace=True)
    df['xe2'] = ta.EMA(df['xe1'], timeperiod=length)
    df['xe2'].fillna(0, inplace=True)
    df['xe3'] = ta.EMA(df['xe2'], timeperiod=length)
    df['xe3'].fillna(0, inplace=True)
    df['xe4'] = ta.EMA(df['xe3'], timeperiod=length)
    df['xe4'].fillna(0, inplace=True)
    df['xe5'] = ta.EMA(df['xe4'], timeperiod=length)
    df['xe5'].fillna(0, inplace=True)
    df['xe6'] = ta.EMA(df['xe5'], timeperiod=length)
    df['xe6'].fillna(0, inplace=True)
    b = 0.7
    c1 = -b * b * b
    c2 = 3 * b * b + 3 * b * b * b
    c3 = -6 * b * b - 3 * b - 3 * b * b * b
    c4 = 1 + 3 * b + b * b * b + 3 * b * b
    df['T3Average'] = c1 * df['xe6'] + c2 * df['xe5'] + c3 * df['xe4'] + c4 * df['xe3']

    return df['T3Average']


def pivot_points(dataframe: DataFrame, mode='fibonacci') -> Series:
    if mode == 'simple':
        hlc3_pivot = (dataframe['high'] + dataframe['low'] + dataframe['close']).shift(1) / 3
        res1 = hlc3_pivot * 2 - dataframe['low'].shift(1)
        sup1 = hlc3_pivot * 2 - dataframe['high'].shift(1)
        res2 = hlc3_pivot + (dataframe['high'] - dataframe['low']).shift()
        sup2 = hlc3_pivot - (dataframe['high'] - dataframe['low']).shift()
        res3 = hlc3_pivot * 2 + (dataframe['high'] - 2 * dataframe['low']).shift()
        sup3 = hlc3_pivot * 2 - (2 * dataframe['high'] - dataframe['low']).shift()
        return hlc3_pivot, res1, res2, res3, sup1, sup2, sup3
    elif mode == 'fibonacci':
        hlc3_pivot = (dataframe['high'] + dataframe['low'] + dataframe['close']).shift(1) / 3
        hl_range = (dataframe['high'] - dataframe['low']).shift(1)
        res1 = hlc3_pivot + 0.382 * hl_range
        sup1 = hlc3_pivot - 0.382 * hl_range
        res2 = hlc3_pivot + 0.618 * hl_range
        sup2 = hlc3_pivot - 0.618 * hl_range
        res3 = hlc3_pivot + 1 * hl_range
        sup3 = hlc3_pivot - 1 * hl_range
        return hlc3_pivot, res1, res2, res3, sup1, sup2, sup3
    elif mode == 'DeMark':
        demark_pivot_lt = (dataframe['low'] * 2 + dataframe['high'] + dataframe['close'])
        demark_pivot_eq = (dataframe['close'] * 2 + dataframe['low'] + dataframe['high'])
        demark_pivot_gt = (dataframe['high'] * 2 + dataframe['low'] + dataframe['close'])
        demark_pivot = np.where((dataframe['close'] < dataframe['open']), demark_pivot_lt,
                                np.where((dataframe['close'] > dataframe['open']), demark_pivot_gt, demark_pivot_eq))
        dm_pivot = demark_pivot / 4
        dm_res = demark_pivot / 2 - dataframe['low']
        dm_sup = demark_pivot / 2 - dataframe['high']
        return dm_pivot, dm_res, dm_sup


def heikin_ashi(dataframe, smooth_inputs=False, smooth_outputs=False, length=10):
    df = dataframe[['open', 'close', 'high', 'low']].copy().fillna(0)
    if smooth_inputs:
        df['open_s'] = ta.EMA(df['open'], timeframe=length)
        df['high_s'] = ta.EMA(df['high'], timeframe=length)
        df['low_s'] = ta.EMA(df['low'], timeframe=length)
        df['close_s'] = ta.EMA(df['close'], timeframe=length)

        open_ha = (df['open_s'].shift(1) + df['close_s'].shift(1)) / 2
        high_ha = df.loc[:, ['high_s', 'open_s', 'close_s']].max(axis=1)
        low_ha = df.loc[:, ['low_s', 'open_s', 'close_s']].min(axis=1)
        close_ha = (df['open_s'] + df['high_s'] + df['low_s'] + df['close_s']) / 4
    else:
        open_ha = (df['open'].shift(1) + df['close'].shift(1)) / 2
        high_ha = df.loc[:, ['high', 'open', 'close']].max(axis=1)
        low_ha = df.loc[:, ['low', 'open', 'close']].min(axis=1)
        close_ha = (df['open'] + df['high'] + df['low'] + df['close']) / 4

    open_ha = open_ha.fillna(0)
    high_ha = high_ha.fillna(0)
    low_ha = low_ha.fillna(0)
    close_ha = close_ha.fillna(0)

    if smooth_outputs:
        open_sha = ta.EMA(open_ha, timeframe=length)
        high_sha = ta.EMA(high_ha, timeframe=length)
        low_sha = ta.EMA(low_ha, timeframe=length)
        close_sha = ta.EMA(close_ha, timeframe=length)

        return open_sha, close_sha, low_sha
    else:
        return open_ha, close_ha, low_ha


# Range midpoint acts as Support
def is_support(row_data) -> bool:
    conditions = []
    for row in range(len(row_data) - 1):
        if row < len(row_data) // 2:
            conditions.append(row_data[row] > row_data[row + 1])
        else:
            conditions.append(row_data[row] < row_data[row + 1])
    result = reduce(lambda x, y: x & y, conditions)
    return result


# Range midpoint acts as Resistance
def is_resistance(row_data) -> bool:
    conditions = []
    for row in range(len(row_data) - 1):
        if row < len(row_data) // 2:
            conditions.append(row_data[row] < row_data[row + 1])
        else:
            conditions.append(row_data[row] > row_data[row + 1])
    result = reduce(lambda x, y: x & y, conditions)
    return result


def range_percent_change(dataframe: DataFrame, method, length: int) -> float:
    """
    Rolling Percentage Change Maximum across interval.

    :param dataframe: DataFrame The original OHLC dataframe
    :param method: High to Low / Open to Close
    :param length: int The length to look back
    """
    if method == 'HL':
        return (dataframe['high'].rolling(length).max() - dataframe['low'].rolling(length).min()) / dataframe[
            'low'].rolling(length).min()
    elif method == 'OC':
        return (dataframe['open'].rolling(length).max() - dataframe['close'].rolling(length).min()) / dataframe[
            'close'].rolling(length).min()
    else:
        raise ValueError(f"Method {method} not defined!")

