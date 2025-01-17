"""
Created on Mon Oct 05 13:43:10 2020

@Author: Kaifeng

@Contact: kaifeng.zou@unistra.fr

main function 
"""
import argparse
from config_parser import read_config
from data import MeshData, listMeshes, save_obj
import json
from model import get_model, classifier_, save_model
import numpy as np
import os
import plotLosses
from sklearn.model_selection import train_test_split, RepeatedStratifiedKFold
import time
import torch
import torch.nn.functional as F
import torch_geometric
from torch_geometric.loader import DataLoader
from transform import Normalize
from utils import *

def train(model, train_loader, optimizer, device, checkpoint_dir):
    model.train()
    norm_dict = np.load(os.path.join(checkpoint_dir, 'norm.npz'), allow_pickle = True)
    mean = torch.FloatTensor(norm_dict['mean'])
    std = torch.FloatTensor(norm_dict['std'])

    total = 0
    total_loss = 0
    total_rec_loss = 0
    total_kld = 0
    total_error = 0
    total_correct = 0

    for data in train_loader:

        x,x_gt, y, filename, gt_mesh , R,m,s = data
        x, x_gt = x.to(device), x_gt.to(device)
        sex_hot = F.one_hot(y, num_classes = 2).to(device)
        batch_size = x.num_graphs
        total += batch_size

        optimizer.zero_grad()
        loss, correct, out, z, y_hat = model(x, x_gt, sex_hot, m_type = "train")

        kld = z[0].mean()
        rec_loss = z[1].mean()
        loss.backward()
        optimizer.step()

        total_loss += loss.cpu().detach().numpy() * batch_size
        total_kld += kld.cpu().detach().numpy() * batch_size 
        total_rec_loss += rec_loss.cpu().detach().numpy() * batch_size 
        total_correct += correct

        recon_mesh = out.cpu() * std + mean
        s = s.unsqueeze(1)
        recon_mesh = torch.bmm(recon_mesh * s, R) + m  #procrust
        recon_mesh = recon_mesh.detach().cpu().numpy()
        gt_mesh = gt_mesh.detach().numpy()
        diff = euclidean_distances(recon_mesh, gt_mesh).mean()
        total_error += diff * batch_size

    return total_loss / total, total_kld/total, total_rec_loss/total, total_error/total, total_correct/total

def evaluate(n, model, test_loader, device, faces = None, checkpoint_dir = None, vis = False):
    model.eval()
    norm_dict = np.load(os.path.join(checkpoint_dir, 'norm.npz'), allow_pickle = True)
    mean = torch.FloatTensor(norm_dict['mean'])
    std = torch.FloatTensor(norm_dict['std'])

    total = 0
    total_loss = 0
    total_rec_loss = 0
    total_kld = 0
    first = True
    errors = 0
    total_correct = 0
    acc = 0

    if vis:
        save_path = os.path.join(checkpoint_dir, "mesh"+str(n))    
        if not os.path.exists(save_path):
            os.makedirs(save_path)
        sucess_path = os.path.join(save_path, "sex_change_S")
        failed_path = os.path.join(save_path, "sex_change_F") 
        if not os.path.exists(sucess_path):
            os.makedirs(sucess_path)
        if not os.path.exists(failed_path):
            os.makedirs(failed_path)

    with torch.no_grad():
        for data in test_loader:
            x,x_gt, y, f, gt_mesh , R,m,s = data
            x, x_gt = x.to(device), x_gt.to(device)
            sex_hot = F.one_hot(y, num_classes = 2).to(device)
            loss, correct, out, z, y_hat = model(x, x_gt, sex_hot, m_type = "test")
            batch_size = x.num_graphs
            total += batch_size

            kld = z[0].mean()
            rec_loss = z[1].mean()
            total_loss +=  loss.cpu().numpy() * batch_size
            total_rec_loss += rec_loss.cpu().numpy() * batch_size
            total_kld += kld.cpu().numpy() * batch_size

            recon_mesh = out.cpu() * std + mean
            s = s.unsqueeze(1)
            recon_mesh = torch.bmm(recon_mesh * s, R) + m
            recon_mesh = recon_mesh.detach().cpu().numpy()
            gt_mesh = gt_mesh.detach().cpu().numpy()
            total_correct += correct.cpu().numpy()
            diff = euclidean_distances(recon_mesh, gt_mesh)
            if first : errors = diff
            else : errors = np.concatenate( ( errors, diff ), axis = 0 )
            first = False
            oppo = 1 - sex_hot
            index_gt = torch.argmax(oppo,  dim = 1)
            z = z[2]
            oppo_x =  model.sample(oppo, z)
            index_pred = classifier_(model, oppo_x)

            acc += (index_pred.squeeze() == index_gt.squeeze()).sum().item()

            oppo_mesh =  oppo_x.cpu() * std + mean
            oppo_mesh = torch.bmm(oppo_mesh * s, R) + m
            oppo_mesh = oppo_mesh.detach().cpu().numpy()

            if not vis: continue
       
            for i in range(batch_size):
                file = f[i].split('/')[-1]
                file = file.split('.')[0]

                if index_pred[ i ] == index_gt[ i ] : o_path = sucess_path
                else : o_path = failed_path

                recon_path = os.path.join(o_path, file+'_recon'+'.obj')
                save_obj(recon_path, recon_mesh[i], faces)

                gt_path = os.path.join(o_path, file+'_gt'+'.obj')
                save_obj(gt_path, gt_mesh[i], faces)

                oppo_path = os.path.join(o_path, file+'.obj')
                save_obj(oppo_path, oppo_mesh[i], faces)
                
    return total_loss/total, total_kld/total, total_rec_loss/total, total_correct/total, errors, acc/total

def main(args):

    if not os.path.exists(args.conf):
        print('Config not found' + args.conf)
    print(args.conf)
    config = read_config(args.conf)

    if args.parameter :
        for option in args.parameter:
            value = option[ 1 ]
            if not isinstance( config[ option[ 0 ] ], str ) :
                value = json.loads( value )
            config[ option[ 0 ] ] = value

    print('Initializing parameters')

    checkpoint_dir = config['checkpoint_dir']
    if not os.path.exists(checkpoint_dir):
        os.makedirs(checkpoint_dir)

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    if args.cpu : device = 'cpu'
    print("Using device:",device)

    log_path = config['log_file']
    random_seeds = config['random_seeds']
    n_splits = config['folds']
    test_size = config['test_size']
    lr = config['learning_rate']
    lr_decay = config['learning_rate_decay']
    weight_decay = config['weight_decay']
    total_epochs = config['epoch']
    opt = config['optimizer']
    batch_size = config['batch_size']
    torch_geometric.seed_everything(random_seeds)

    my_log = open(log_path, 'w')

    print('model type:', config['type'], file = my_log)
    print('optimizer type', opt, file = my_log)
    print('learning rate:', lr, file = my_log)

    start_epoch = 1

    checkpoint_file = config['checkpoint_file']
    print(checkpoint_file)
    if checkpoint_file:
        checkpoint = torch.load(checkpoint_file)
        start_epoch = checkpoint['epoch_num']
        net.load_state_dict(checkpoint['state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer'])
        #To find if this is fixed in pytorch
        for state in optimizer.state.values():
            for k, v in state.items():
                if isinstance(v, torch.Tensor):
                    state[k] = v.to(device)

    dataset_index, labels = listMeshes( config )

    skf = RepeatedStratifiedKFold(n_splits=n_splits, n_repeats=1, random_state = random_seeds)

    n = 0
    y = np.ones(len(dataset_index))

    for train_index, test_index in skf.split(dataset_index, y):
        train_, valid_index = train_test_split(np.array(dataset_index)[train_index], test_size=test_size, random_state = random_seeds)
        history = []
        print('loading template...', config['template'])
        net, template_mesh = get_model(config, device)
        template = np.array(template_mesh.v)
        faces = np.array(template_mesh.f)
        optimizer = torch.optim.Adam(net.parameters(), lr=lr, weight_decay=weight_decay)
        n+=1

        if args.train:
            train_dataset = MeshData(train_, config, labels, dtype = 'train', template = template, pre_transform = Normalize())
            train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True)

            valid_dataset = MeshData(valid_index, config, labels, dtype = 'test', template = template, pre_transform = Normalize())
            valid_loader = DataLoader(valid_dataset, batch_size=batch_size, shuffle=True)
            best_loss = 10000000
            best_sex_change_success_rate = -1

            for epoch in range(start_epoch, total_epochs + 1):

                begin = time.time()

                for e_index, e in enumerate( config['learning_rates_epochs'] ):
                    if epoch > e:
                        for p in optimizer.param_groups:
                            p['lr'] = config['learning_rates'][ e_index ]

                train_loss, train_kld, train_rec_loss, train_error, train_acc = train(net, train_loader, optimizer, device,checkpoint_dir = checkpoint_dir)

                valid_loss, valid_kld, valid_rec_loss, valid_acc, error, acc  = evaluate(n, net, valid_loader, device, checkpoint_dir = checkpoint_dir)
                mean_val_error = error.mean().item()

                duration = time.time() - begin

                history.append( {
                    "epoch" : epoch,
                    "begin" : begin,
                    "duration" : duration,
                    "training" : {
                        "loss" : train_loss,
                        "kld" : train_kld,
                        "reconstruction_loss" : train_rec_loss,
                        "accuracy" : train_acc.item(),
                        "error" : train_error
                    },
                    "validation" : {
                        "loss" : valid_loss,
                        "kld" : valid_kld,
                        "reconstruction_loss" : valid_rec_loss,
                        "accuracy" : valid_acc.item(),
                        "error" : mean_val_error,
                        "sex_change_success_rate" : acc
                    }
                } )

                if config[ "save" ] == "best_loss":
                    if valid_loss <= best_loss:
                        save_model(net, optimizer, n, train_loss, valid_loss, checkpoint_dir)
                        best_loss = valid_loss
                        history[ -1 ][ "saved" ] = True

                if config[ "save" ] == "best_sex_change_success_rate":
                    if best_sex_change_success_rate <= acc:
                        save_model(net, optimizer, n, train_loss, valid_loss, checkpoint_dir)
                        best_sex_change_success_rate = acc
                        history[ -1 ][ "saved" ] = True

                if epoch%10 == 0:
                    toPrint = 'Epoch {}, train loss {}(kld {}, recon loss {}, train acc {}) || valid loss {}(error {}, rec_loss {}, valid acc {}, sex change acc {})'
                    toPrint = toPrint.format(epoch, train_loss,train_kld, train_rec_loss, train_acc, valid_loss, mean_val_error, valid_rec_loss, valid_acc, acc)
                    print( toPrint )
                    print( toPrint, file = my_log)

            if config[ "save" ] == "last":
                save_model(net, optimizer, n, train_loss, valid_loss, checkpoint_dir)
                history[ -1 ][ "saved" ] = True

        else : history.append( {} )

        if args.test:
            test_dataset = MeshData(np.array(dataset_index)[test_index], config, labels, dtype = 'test', template = template, pre_transform = Normalize())
            test_loader = DataLoader(test_dataset, batch_size=batch_size, shuffle=False)
            checkpoint_file = os.path.join(checkpoint_dir, 'checkpoint_'+ str(n)+'.pt')
            checkpoint = torch.load(checkpoint_file)
            net.load_state_dict(checkpoint['state_dict'])

            test_loss, test_kld, test_rec_loss, cls_acc, test_error, acc = evaluate(n, net, test_loader,device, faces = faces, checkpoint_dir = checkpoint_dir, vis = args.vis)
            print(test_error.shape)
            toPrint = 'round {} test loss {},  mean error: {}, train sigma {}, classification acc {}, sex change rate {}'
            toPrint = toPrint.format( n, test_loss, np.mean(test_error), np.std(test_error), cls_acc, acc )
            print( toPrint )
            print( toPrint, file = my_log )
            history[ -1 ][ "test" ] = {
                "loss" : test_loss,
                "kld" : test_kld,
                "reconstruction_loss" : test_rec_loss,
                "accuracy" : cls_acc.item(),
                "error" : np.mean( test_error ).item(),
                "sex_change_success_rate" : acc
            }

        with open(os.path.join(checkpoint_dir, 'history' + str( n ) + '.json'), 'w') as fp:
            json.dump(history, fp)

        plt = plotLosses.plotLosses( "Fold " + str( n ), history, config )
        plt.savefig( os.path.join( checkpoint_dir, 'losses' + str( n ) + '.pdf') )

if __name__ == '__main__':

    parser = argparse.ArgumentParser(description='Pytorch Trainer', formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('-c', '--conf', help='path of config file')
    parser.add_argument( "-p", "--parameter", metavar=('parameter', 'value'), action='append', nargs=2, help = "config parameters" )
    parser.add_argument('-t', '--train',action='store_true')
    parser.add_argument('-s', '--test',action='store_true')
    parser.add_argument('--cpu',action='store_true', help = "force cpu")
    parser.add_argument('-v', '--vis',action='store_true', help = "save transformed meshes")
    args = parser.parse_args()

    if args.conf is None:
        args.conf = os.path.join(os.path.dirname(__file__), './files/default.cfg')
        print('configuration file not specified, trying to load '
              'it from current directory', args.conf)
    main(args)
