"""
Created on Mon Oct 05 13:43:10 2020

@Author: Kaifeng

@Contact: kaifeng.zou@unistra.fr

main function 
"""
import argparse
import os
import torch
import numpy as np
import torch.nn.functional as F
#from torch.utils.data import Dataset, DataLoader
from torch_geometric.data import Dataset, DataLoader
import torch_geometric
import mesh_operations
from config_parser import read_config
from data import MeshData, listMeshes, save_obj
from model import get_model, classifier_, save_model
from transform import Normalize
from utils import *
from psbody.mesh import Mesh
from sklearn.model_selection import train_test_split, RepeatedStratifiedKFold
import copy
import time
import json

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

def train(model, dvae, train_loader, optimizer, device, criterion):
    model.train()
    total_loss = 0
    total = 0
    correct = 0
    for data in train_loader:
        x,x_gt, label, f, gt_mesh , R,m,s = data
        x_gt, label = x_gt.to(device).float(), label.to(device)
        diff, _ = estimate_diff(dvae, x_gt, label, "train")
        optimizer.zero_grad()
        pred = model(diff)
        loss = criterion(pred, label)
        loss.backward()
        optimizer.step()
        batch_size = label.shape[0]
        total_loss += loss.cpu().detach().numpy() * batch_size
       # print(torch.nn.functional.softmax(pred))
        #predicted = torch.argmax(torch.nn.functional.softmax(pred), dim = -1)
        predicted = torch.argmax(F.softmax(pred), dim = -1)
      #  print(predicted)
        total += batch_size
        correct += (predicted == label).sum().item()

    return total_loss / total, correct / total

def evaluate(model, dvae, test_loader, device, criterion, err_file = False):
    model.eval()
    dvae.eval()
    total_loss = 0
    total = 0
    correct = 0
    err = {}
    predicted_dist = np.empty((0))
    with torch.no_grad():
        for data in test_loader:
            x,x_gt, label, f, gt_mesh , R,m,s = data
            x_gt, label = x_gt.to(device).float(), label.to(device)
            diff, _ = estimate_diff(dvae, x_gt, label, "test")
            pred = model(diff)
            batch_size = label.shape[0]
            loss = criterion(pred, label)
            total_loss +=  loss.cpu().numpy() * batch_size
            predicted =torch.argmax(F.softmax(pred), dim = -1)
            total += batch_size
            correct += (predicted == label).sum().item()

            if err_file == True:
                predicted = predicted.cpu().numpy().squeeze(1)
                label = label.cpu().numpy().squeeze(1)
                pred = pred.cpu().numpy().squeeze(1)
                f = np.array(f)
                error_ind = np.where((predicted == label))

                for idx in range(label.shape[0]):
                    if predicted[idx] != label[idx]:
                        err.update({f[idx]: str(predicted[idx])})

    return total_loss / total, correct/total, err

def estimate_diff(net, x, y,dtype):
    net = net.to(device)
    ori = copy.copy(x)
    if len(x.shape) == 2:
        x = x.reshape(1, -1, 3).to(device)
        y = torch.tensor(y).unsqueeze(0).to(device)

    with torch.no_grad():
        x = net.encoder(x)
        y_hat = net.classifier(x)    
        index_pred = torch.argmax(y_hat,  dim = 1)
    
        correct = torch.sum(index_pred == y).item()

        if dtype != "train":
            sex_hot = F.one_hot(index_pred, num_classes = 2)
            x = torch.cat([sex_hot, x], -1)
        else:
            
            sex_hot =  F.one_hot(y, num_classes = 2)
            x = torch.cat([sex_hot, x], -1)

        x_mean = net.z_mean(x)

        #sex_hot = #_m = F.one_hot(torch.ones_like(y).to(device), num_classes = 2)
        recon =  net.sample(sex_hot, x_mean)

        oppo = 1-sex_hot
        recon_oppo =  net.sample(oppo, x_mean)


        diff_1 = ori - recon_oppo
        diff_2 = ori - recon

        diff = torch.cat((diff_1, diff_2), dim=-1)#diff_1 + diff_2


    return diff, correct


def main(args):

    if not os.path.exists(args.conf):
        print('Config not found' + args.conf)
    print(args.conf)
    config = read_config(args.conf)

    print('Initializing parameters')

    checkpoint_dir = config['checkpoint_dir']
    if not os.path.exists(checkpoint_dir):
        os.makedirs(checkpoint_dir)

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    if args.cpu : device = 'cpu'
    print("Using device:",device)

    random_seeds = config['random_seeds']
    torch_geometric.seed_everything(random_seeds)
    lr = config['learning_rate']
    lr_decay = config['learning_rate_decay']
    weight_decay = config['weight_decay']
    total_epochs = config['epoch']
    opt = config['optimizer']
    batch_size = config['batch_size']

    checkpoint_file = config['checkpoint_file']
    dvae = get_model(config, device, model_type="cheb_VAE", save_init = False)
    print("loading checkpoint for DVAE from ", checkpoint_file)
  
    checkpoint = torch.load(checkpoint_file)
    dvae.load_state_dict(checkpoint['state_dict'])   

    print('loading template...', config['template'])
    template_mesh = Mesh(filename=config['template'])
    template = np.array(template_mesh.v)
    faces = np.array(template_mesh.f)
    #criterion = BCEFocalLoss()
    my_log = open(config['log_file'], 'w')

    print('model type:', config['type'], file = my_log)
    print('optimizer type', opt, file = my_log)
    print('learning rate:', lr, file = my_log)

    print(checkpoint_file)
    criterion = torch.nn.CrossEntropyLoss()
    dataset_index, labels = listMeshes( config )
    skf = RepeatedStratifiedKFold(n_splits=config['folds'], n_repeats=1, random_state = random_seeds)
    n = 0
    y = np.ones(len(dataset_index))

    for train_index, test_index in skf.split(dataset_index, y):
        train_, valid_index = train_test_split(np.array(dataset_index)[train_index], test_size=config['test_size'], random_state = random_seeds)

        history = []
        net = get_model(config, device, model_type="cheb_GCN")
        optimizer = torch.optim.Adam(net.parameters(), lr=lr, weight_decay=weight_decay)
        n+=1

        if args.train:

            best_val_acc = 0
            train_dataset = MeshData(train_, config, labels, dtype = 'train', template = template, pre_transform = Normalize())
            train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True, num_workers=4)

            valid_dataset = MeshData(valid_index, config, labels, dtype = 'test', template = template, pre_transform = Normalize())
            valid_loader = DataLoader(valid_dataset, batch_size=batch_size, shuffle=True, num_workers=0)

            for epoch in range(1, total_epochs + 1):
                begin = time.time()
                train_loss, train_acc = train(net, dvae, train_loader, optimizer, device, criterion)
                val_loss, valid_acc, _ = evaluate(net,dvae, valid_loader, device, criterion)

                if valid_acc >= best_val_acc:
                    save_model(net, optimizer, n, train_loss, val_loss, checkpoint_dir)
                    best_val_acc = valid_acc

                duration = time.time() - begin
                print('epoch ', epoch,' Train loss ', train_loss, 'train acc',train_acc, ' Val loss ', val_loss, 'acc ', valid_acc)
                print('epoch ', epoch,' Train loss ', train_loss, 'train acc',train_acc, ' Val loss ', val_loss, 'acc ', valid_acc, file = my_log)

                history.append( {
                    "epoch" : epoch,
                    "begin" : begin,
                    "duration" : duration,
                    "training" : {
                        "loss" : train_loss,
                        "accuracy" : train_acc
                    },
                    "validation" : {
                        "loss" : val_loss,
                        "accuracy" : valid_acc
                    }
                } )

            with open(os.path.join(checkpoint_dir, 'history' + str( n ) + '.json'), 'w') as fp:
                json.dump(history, fp)

        if args.test:
            if not args.train:
                checkpoint_file = os.path.join(checkpoint_dir, 'checkpoint_'+ str(n)+'.pt')
                checkpoint = torch.load(checkpoint_file)
                net.load_state_dict(checkpoint['state_dict'])

            test_dataset = MeshData(np.array(dataset_index)[test_index], config, labels, dtype = 'test', template = template, pre_transform = Normalize())  
            test_loader = DataLoader(test_dataset, batch_size=batch_size, shuffle=False, num_workers=0)
            test_loss, test_acc, _ = evaluate(net, dvae, test_loader, device, criterion, err_file = False)

            print( 'test loss ', test_loss, 'test acc',test_acc)


if __name__ == '__main__':

    parser = argparse.ArgumentParser(description='Pytorch Trainer')
    parser.add_argument('-c', '--conf', help='path of config file')
    parser.add_argument('-t', '--train',action='store_true')
    parser.add_argument('-s', '--test',action='store_true')
    parser.add_argument('--cpu',action='store_true', help = "force cpu")
    args = parser.parse_args()

    if args.conf is None:
        args.conf = os.path.join(os.path.dirname(__file__), './files/default.cfg')
        print('configuration file not specified, trying to load '
              'it from current directory', args.conf)
    main(args)
