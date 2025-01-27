import numpy as np
import pandas as pd
from datetime import datetime, timedelta
import joblib
import json
import os
import sys

import argparse
import logging
import time

def check_dir(file_name):
    dir_path = os.path.dirname(file_name)
    if not os.path.exists(dir_path):
        print('make dirs:', dir_path)
        os.makedirs(dir_path)

def add_args(parser):
    parser.add_argument('--log_file', type=str, default='',
                        help='Logging file save path')
    parser.add_argument('--verbose', type=int, default=logging.INFO,
                        help='Logging Level, 0, 10, ..., 50')
    parser.add_argument('--data_path', type=str, default='',help='Path for dataset.')
    parser.add_argument('--save_path', type=str, default='',help='Path to save features.')
    parser.add_argument('--cox_feature_path',type=str,default='cox_dataset/fold-XXX/D-Cox-Time/',help="Path for cox results, where 'XXX' will be replace by fold num.")
    parser.add_argument('--fold_num', type=int, default=5)
    return parser

class Features_generator:
    def __init__(self,args):
        self.data_path = args.data_path
        self.save_path = args.save_path
        os.makedirs(self.save_path,exist_ok=True)
        self.cox_feature_path = args.cox_feature_path
        self.fold_num = args.fold_num

    def data_load(self):
        data_path = self.data_path
        logging.info("Loading data from {} ...".format(data_path))
        self.all_features = pd.read_csv(os.path.join(data_path,"day_features.csv"))
        self.user_label = pd.read_csv(os.path.join(data_path,"labels.csv"))
        save_param = os.path.join(data_path,"Difficulty_Flow/0_difficulty_flow.json")
        with open(save_param) as F:
            curve_user = json.load(F)
        user_ids = [int(uid) for uid in list(curve_user.keys())]
        a = [curve_user[str(x)][0] for x in user_ids]
        b = [curve_user[str(x)][1] for x in user_ids]
        self.flow_features = pd.DataFrame(np.array([user_ids,a,b]).T,columns=["user_id","a","b"])
        
        self.beta_diff, self.beta_scale = [], []
        for fold in range(self.fold_num):
            save_beta = os.path.join(self.cox_feature_path.replace("XXX",str(2)),"diff_beta.npy") 
            save_scale = os.path.join(self.cox_feature_path.replace("XXX",str(2)),"diff_scale.npy") 
            self.beta_diff.append(np.load(save_beta))
            self.beta_scale.append(np.load(save_scale))

    def sample_construct(self):
        logging.info("Constructing sampled data...")
        sample_features = []
        columns = list(self.all_features.columns)
        columns += ["start_day","end_day","label"]
        for i,u in (self.user_label.iterrows()):
            part_features = self.all_features.loc[(self.all_features.user_id==u.user_id)&
                                     (self.all_features.day_since_register<u.end_day)&
                                     (self.all_features.day_since_register>=u.start_day)].copy()
            part_features["label"] = u.label
            part_features["start_day"] = int(u.start_day)
            part_features["end_day"] = int(u.end_day)
            sample_features += list(part_features[columns].to_numpy())
        
        sample_features_np = np.array(sample_features)
        sample_features = pd.DataFrame(sample_features_np, columns=columns)
        sample_features["retry_time"] = sample_features.retry_time.astype(float)
        sample_features["global_retrytime"] = sample_features.global_retrytime.astype(float)
        sample_features["start_day"] = sample_features.start_day.astype(float)
        sample_features["end_day"] = sample_features.end_day.astype(float)
        self.sample_features = sample_features

    def basic_features(self):
        logging.info("Generating basic features...")
        self.sample_features["interval_length"] = self.sample_features["end_day"] - self.sample_features["start_day"]
        day_features = self.sample_features.groupby("user_id").agg({"duration":"sum","global_retrytime":"count",
                                     "play_num":"sum","level_num":"sum",  "interval_length":"max",
                                    "session_num":"sum","gold_amount":"sum","coin_amount":"sum","label":"max"})
        for feature in ["duration","play_num","level_num","session_num"]:
            day_features[feature] = day_features[feature].astype(float) / day_features["interval_length"]
        day_features.rename(columns={"global_retrytime":"day_login"},inplace=True)
        day_features["frequency"] = day_features["day_login"].astype(float)/day_features["interval_length"]
        # first purchase interval，last purchase interval 
        first_purchase = self.sample_features.loc[(self.sample_features.gold_amount>0)|(self.sample_features.coin_amount>0)].groupby("user_id").head(1)
        last_purchase = self.sample_features.loc[(self.sample_features.gold_amount>0)|(self.sample_features.coin_amount>0)].groupby("user_id").tail(1)
        first_purchase.rename(columns={"day_since_register":"first_interval"},inplace=True)
        first_purchase["first_interval"] = first_purchase["first_interval"].astype(float) - first_purchase["start_day"].astype(float)
        last_purchase["last_interval"] = last_purchase["end_day"].astype(float)-last_purchase["day_since_register"].astype(float)

        day_features = day_features.merge(first_purchase.loc[:,["user_id","first_interval"]],on="user_id",how="left")
        day_features = day_features.merge(last_purchase.loc[:,["user_id","last_interval"]],on="user_id",how="left")
        day_features.fillna(-1,inplace=True)
        day_features = day_features.drop(columns=["day_login"])
        
        logging.info("Saving basic features...")
        day_features.to_csv(os.path.join(self.save_path,"feature_data.csv"),index=False)
        
        self.day_features = day_features

    def diff_features(self):
        logging.info("Generating difficulty features...")
        
        # difficulty-related features
        diff_features = self.sample_features.groupby("user_id").agg({"retry_time":["mean","var"],"global_retrytime":["mean","var"]}).reset_index()
        diff_features.columns = ["user_id","retry_time","var_retry","global_retrytime","var_globalretry"]
        diff_lastday_features = self.sample_features.groupby("user_id").tail(1).loc[:,["user_id","retry_time","global_retrytime"]]
        diff_lastday_features.rename(columns={"retry_time":"lastday_retry","global_retrytime":"lastday_globalretry"},inplace=True)
        diff_features = diff_features.merge(diff_lastday_features,on=["user_id"],how="left")
        diff_features = diff_features.merge(self.day_features,on=["user_id"],how="left")
        diff_features.fillna(0,inplace=True)
        
        # add PPD
        sample_features_diff = self.sample_features.copy()
        sample_features_diff = sample_features_diff.merge(self.flow_features,on="user_id",how="left")
        sample_features_diff.fillna(0,inplace=True)
        
        d_value = []
        for i,row in (sample_features_diff.iterrows()):
            d = row.retry_time - row.a*row.global_retrytime - row.b
            d_value.append(d)
        sample_features_diff["PPD"] = d_value
        
        d_features = sample_features_diff.groupby(["user_id"]).agg({"PPD":["mean","var"]}).reset_index()
        d_features.columns=["user_id","mean_d","var_d"]
        d_last_features = sample_features_diff.groupby(["user_id"]).tail(1)

        d_features = d_features.merge(d_last_features.loc[:,["user_id","PPD"]],on="user_id" ,how="left")
        d_features.rename(columns={"PPD":"last_d"},inplace=True)
        diff_features = d_features.merge(diff_features,on="user_id",how="right")
        diff_features.fillna(0,inplace=True)

        logging.info("Saving difficulty features...")
        diff_features.to_csv(os.path.join(self.save_path,"feature_data_diff.csv"),index=False)
        self.diff_features = diff_features
        self.sample_features_diff = sample_features_diff

    def pd_features(self):
        logging.info("Generating DDI features...")
        for fold in range(self.fold_num):
            logging.info("--- Fold %d"%(fold+1))
            R = self.beta_scale[fold]
            def Range(d):
                for i,r in enumerate(R):
                    if r>d:
                        return i
                return len(R)
            cox_value = []
            for i,row in (self.sample_features_diff.iterrows()):
                d = row.retry_time - row.a*row.global_retrytime - row.b
                cox = self.beta_diff[fold][int(row.day_since_register),Range(d)]
                cox_value.append(cox)
            self.sample_features_diff["beta"] = cox_value
            
            beta_features = self.sample_features_diff.groupby(["user_id"]).agg({"beta":["mean","var"]}).reset_index()
            beta_features.columns=["user_id","mean_beta","var_beta"]
            beta_last_features = self.sample_features_diff.groupby(["user_id"]).tail(1)
            beta_features = beta_features.merge(beta_last_features.loc[:,["user_id","beta"]],on="user_id" ,how="left")
            beta_features.rename(columns={"beta":"last_beta"},inplace=True)
            beta_features = beta_features.merge(self.diff_features,on=["user_id"],how="right")
            beta_features.fillna(0,inplace=True)
            beta_features.to_csv(os.path.join(self.save_path,"feature_data_all_fold-%d.csv"%(fold+1)),index=False)
        

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser = add_args(parser)
    args, extras = parser.parse_known_args()

    if args.log_file == '':
        args.log_file = 'logs/{}/{}.txt'.format("feature_based_data",args.data_path+".log")

    check_dir(args.log_file)
    logging.basicConfig(filename=args.log_file, level=args.verbose)
    logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))

    generator = Features_generator(args)
    generator.data_load()
    generator.sample_construct()
    generator.basic_features()
    generator.diff_features()
    generator.pd_features()
    logging.info("All Done!")