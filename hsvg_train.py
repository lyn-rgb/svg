import torch
import torch.optim as optim
import torch.nn as nn
import argparse
import os
import random
from torch.autograd import Variable
from torch.utils.data import DataLoader
import utils
import itertools
import progressbar
from tqdm import tqdm
import numpy as np
from tensorboardX import SummaryWriter

from models.hsvgnet import hsvgnet as HSVG

parser = argparse.ArgumentParser()
parser.add_argument('--lr', default=0.002, type=float, help='learning rate')
parser.add_argument('--beta1', default=0.9, type=float, help='momentum term for adam')
parser.add_argument('--batch_size', default=100, type=int, help='batch size')
parser.add_argument('--log_dir', default='logs/hsvg/', help='base directory to save logs')
parser.add_argument('--model_dir', default='', help='base directory to save logs')
parser.add_argument('--name', default='', help='identifier for directory')
parser.add_argument('--data_root', default='data', help='root directory for data')
parser.add_argument('--optimizer', default='adam', help='optimizer to train with')
parser.add_argument('--nepochs', type=int, default=300, help='number of epochs to train for')
parser.add_argument('--epoch_size', type=int, default=600, help='epoch size')
parser.add_argument('--seed', default=1, type=int, help='manual seed')

parser.add_argument('--image_width', type=int, default=64, help='the height / width of the input image to network')
parser.add_argument('--channels', default=1, type=int)
parser.add_argument('--dataset', default='smmnist', help='dataset to train with, (smmnist|kth|bair)')
parser.add_argument('--n_past', type=int, default=5, help='number of frames to condition on')
parser.add_argument('--n_future', type=int, default=10, help='number of frames to predict during training')
parser.add_argument('--n_eval', type=int, default=30, help='number of frames to predict during eval')
parser.add_argument('--n_level', type=int, default=1, help='number of levels in the hierachy')
parser.add_argument('--rnn_size', type=int, default=128, help='dimensionality of hidden layer')
parser.add_argument('--prior_rnn_layers', type=int, default=1, help='number of layers')
parser.add_argument('--posterior_rnn_layers', type=int, default=1, help='number of layers')
parser.add_argument('--predictor_rnn_layers', type=int, default=1, help='number of layers')
parser.add_argument('--z_dim', type=int, default=4, help='dimensionality of z_t')
parser.add_argument('--g_dim', type=int, default=128, help='dimensionality of encoder output vector and decoder input vector')
parser.add_argument('--beta', type=float, default=0.0001, help='weighting on KL to prior')
parser.add_argument('--model', default='dcgan', help='model type (dcgan | vgg)')
parser.add_argument('--data_threads', type=int, default=5, help='number of data loading threads')
parser.add_argument('--num_digits', type=int, default=2, help='number of digits for moving mnist')
#parser.add_argument('--last_frame_skip', action='store_true', help='if true, skip connections go between frame t and frame t+t rather than last ground truth frame')

opt = parser.parse_args()

print(opt)
## dir
if opt.dataset == 'smmnist':
    checkpoint_dir = opt.log_dir + opt.dataset +'-{}/'.format(opt.num_digits)+ '/{}-z_dim={}-g_dim={}-n_level={}'.format(opt.model, opt.z_dim, opt.g_dim, opt.n_level)
else:
    checkpoint_dir = opt.log_dir + opt.dataset + '/{}-z_dim={}-g_dim={}-n_level={}'.format(opt.model, opt.z_dim, opt.g_dim, opt.n_level)
if not os.path.exists(opt.log_dir):
    os.mkdir(opt.log_dir)
if not os.path.exists(opt.log_dir+opt.dataset):
    os.mkdir(opt.log_dir+opt.dataset)
if not os.path.exists(checkpoint_dir):
    os.mkdir(checkpoint_dir)
if not os.path.exists(checkpoint_dir+'/gen/'):
    os.mkdir(checkpoint_dir+'/gen/')
if not os.path.exists(checkpoint_dir+'/plots/'):
    os.mkdir(checkpoint_dir+'/plots/')
## end

## dataset
train_data, test_data = utils.load_dataset(opt)

train_loader = DataLoader(train_data,
                          num_workers=opt.data_threads,
                          batch_size=opt.batch_size,
                          shuffle=True,
                          drop_last=True,
                          pin_memory=True)
test_loader = DataLoader(test_data,
                         num_workers=opt.data_threads,
                         batch_size=opt.batch_size,
                         shuffle=False,
                         drop_last=True,
                         pin_memory=True)

def get_training_batch():
    while True:
        for sequence in train_loader:
            batch = utils.normalize_data(opt, dtype, sequence)
            yield batch
training_batch_generator = get_training_batch()

def get_testing_batch():
    while True:
        for sequence in test_loader:
            batch = utils.normalize_data(opt, dtype, sequence)
            yield batch 
testing_batch_generator = get_testing_batch()
## end

## kl loss
def kl_criterion(mu1, logvar1, mu2, logvar2):
    # KL( N(mu_1, sigma2_1) || N(mu_2, sigma2_2)) = 
    #   log( sqrt(
    sigma1 = logvar1.mul(0.5).exp() 
    sigma2 = logvar2.mul(0.5).exp() 
    kld = torch.log(sigma2/sigma1) + (torch.exp(logvar1) + (mu1 - mu2)**2)/(2*torch.exp(logvar2)) - 1/2
    return kld.sum() / opt.batch_size
## end

# --------- plotting funtions ------------------------------------
def plot(model, x, epoch):
    nsample = 20 
    gen_seq = [[x[0]] for i in range(nsample)]
    gt_seq = [x[i] for i in range(len(x))]

    for s in range(nsample):
        ## initialization
        model.init_states(x[0])
        ## prediction
        for i in range(1, opt.n_eval):
            if i < opt.n_past:
                hs_rec, feats, zs, mus, logvars = model.reconstruction(x[i])
                model.skips = feats
                gen_seq[s].append(x[i])
            else:
                x_pred = model.inference()
                gen_seq[s].append(x_pred)

    to_plot = []
    gifs = [ [] for t in range(opt.n_eval) ]
    nrow = min(opt.batch_size, 10)
    for i in range(nrow):
        # ground truth sequence
        row = [] 
        for t in range(opt.n_eval):
            row.append(gt_seq[t][i])
        to_plot.append(row)

        # best sequence
        min_mse = 1e7
        for s in range(nsample):
            mse = 0
            for t in range(opt.n_eval):
                mse +=  torch.sum( (gt_seq[t][i].data.cpu() - gen_seq[s][t][i].data.cpu())**2 )
            if mse < min_mse:
                min_mse = mse
                min_idx = s

        s_list = [min_idx, 
                  np.random.randint(nsample), 
                  np.random.randint(nsample), 
                  np.random.randint(nsample), 
                  np.random.randint(nsample)]
        for ss in range(len(s_list)):
            s = s_list[ss]
            row = []
            for t in range(opt.n_eval):
                row.append(gen_seq[s][t][i]) 
            to_plot.append(row)
        for t in range(opt.n_eval):
            row = []
            row.append(gt_seq[t][i])
            for ss in range(len(s_list)):
                s = s_list[ss]
                row.append(gen_seq[s][t][i])
            gifs[t].append(row)

    fname = '%s/gen/sample_%d.png' % (checkpoint_dir, epoch) 
    utils.save_tensors_image(fname, to_plot)

    fname = '%s/gen/sample_%d.gif' % (checkpoint_dir, epoch) 
    utils.save_gif(fname, gifs)


def plot_rec(model, x, epoch):
    model.init_states(x[0])
    gen_seq = [x[0]]
    for i in range(1, opt.n_past+opt.n_future):
        if i < opt.n_past:
            hs_rec, feats, zs, mus, logvars = model.reconstruction(x[i])
            model.skips = feats
            gen_seq.append(x[i])
        else:
            hs_rec, feats, zs, mus, logvars = model.reconstruction(x[i])
            x_rec = model.decoding(hs_rec)
            gen_seq.append(x_rec)
   
    to_plot = []
    nrow = min(opt.batch_size, 10)
    for i in range(nrow):
        row = []
        for t in range(opt.n_past+opt.n_future):
            row.append(gen_seq[t][i]) 
        to_plot.append(row)
    fname = '%s/gen/rec_%d.png' % (checkpoint_dir, epoch) 
    utils.save_tensors_image(fname, to_plot)

# --------- training funtions ------------------------------------
def train(model, optimizer, x):
    assert(len(x)>2) # at least predict 1 frame based on 2 previous frames
    model.zero_grad()
    # initialize the hidden state.
    model.init_states(x[0])
    x_rec, rec, kld = model(x[1], updata_skips=True)
    
    total_rec = 0
    total_kld = 0
    for i in range(2, opt.n_past+opt.n_future):
        x_rec, rec, kld = model(x[i], updata_skips=(i < opt.n_past))
        total_rec += rec
        total_kld += kld

    loss = total_rec + total_kld*opt.beta
    loss.backward()

    optimizer.step()
    
    return total_rec.data.cpu().numpy()/(opt.n_past+opt.n_future - 2), total_kld.data.cpu().numpy()/(opt.n_future+opt.n_past - 2)

def main():
    # --------- logging ------------------------------------------
    if not os.path.exists(checkpoint_dir+'/train_log'):
        os.mkdir(checkpoint_dir+'/train_log')
    train_writer = SummaryWriter(checkpoint_dir+'/train_log') 
    '''
    if not os.path.exists(checkpoint_dir+'/test_log'):
        os.mkdir(checkpoint_dir+'/test_log')
    test_writer = SummaryWriter(checkpoint_dir+'/test_log')
    '''
    # --------- random seed --------------------------------------
    print("Random Seed: ", opt.seed)
    random.seed(opt.seed)
    torch.manual_seed(opt.seed)
    torch.cuda.manual_seed_all(opt.seed)
    dtype = torch.cuda.FloatTensor
    # --------- define criterion ---------------------------------
    rec_criterion = nn.MSELoss()
    kld_criterion = kl_criterion
    rec_criterion.cuda()
    # --------- define model -------------------------------------
    model = {'in_channels': opt.channels,
             'in_size': opt.image_width,
             'model_type': opt.model,
             'nlevel': opt.n_level,
             'z_dims': [opt.z_dim*(opt.n_level-i) for i in range(opt.n_level)],
             'o_dims': [(opt.g_dim//8)*(opt.n_level-i) for i in range(opt.n_level)],
             'g_dim': opt.g_dim,
             'rnn_size': [opt.rnn_size for i in range(opt.n_level)], 
             'rnnlayers':[opt.prior_rnn_layers for i in range(opt.n_level)],
             'rec_criterion': rec_criterion,
             'kld_criterion': kld_criterion,
            }
    hsvg_net = HSVG(model)
    hsvg_net.cuda()
    # --------- define optimizer ---------------------------------
    if opt.optimizer == 'adam':
        opt.optimizer = optim.Adam
    elif opt.optimizer == 'rmsprop':
        opt.optimizer = optim.RMSprop
    elif opt.optimizer == 'sgd':
        opt.optimizer = optim.SGD
    else:
        raise ValueError('Unknown optimizer: %s' % opt.optimizer)
    hsvg_optimizer = opt.optimizer(hsvg_net.parameters(), lr=opt.lr, betas=(opt.beta1, 0.999))
    hsvg_lr_sched = torch.optim.lr_scheduler.ExponentialLR(hsvg_optimizer, .995)
    # --------- load checkpoint ----------------------------------
    start_epoch = 0
    print('Loading checkpoint ...')
    checkpoint_list = sorted(glob.glob('{}/hsvgnet_ep*.pth.tar'.format(checkpoint_dir)))
    if checkpoint_list:
        checkpoint_path = checkpoint_list[-1]
        checkpoint_dict = torch.load(checkpoint_path)
        start_epoch = checkpoint_dict['epoch'] + 1
        hsvg_net.load_state_dict(checkpoint_dict['hsvg_net'])
        hsvg_optimizer.load_state_dict(checkpoint_dict['hsvg_optimizer'])
        print('Found checkpoint file {}'.format(checkpoint_path))
    else:
        print('No matching checkpoint file found')
    # --------- training loop ------------------------------------
    for epoch in range(start_epoch, opt.nepochs):
        hsvg_net.train()

        epoch_rec = 0
        epoch_kld = 0
        #progress = progressbar.ProgressBar(max_value=opt.epoch_size).start()
        
        info_dict = {'REC Loss':0.0, 'KLD Loss':0.0}
        describe = '[Epoch {:04d}]:'.format(epoch)
        pbar = tqdm(total=opt.epoch_size, desc = describe)
        for i in range(opt.epoch_size):
            #progress.update(i+1)
            x = next(training_batch_generator)

            # train frame_predictor 
            rec, kld = train(hsvg_net, hsvg_optimizer, x)
            
            epoch_rec += rec
            epoch_kld += kld
            
            train_writer.add_scalar('rec loss', rec, i + opt.epoch_size*epoch)
            train_writer.add_scalar('kld loss', kld, i + opt.epoch_size*epoch)
            
            info_dict['REC Loss'] = rec
            info_dict['KLD Loss'] = kld
            pbar.set_postfix(info_dict)
            pbar.update(1)
            

        hsvg_lr_sched.step()
        pbar.close()
        #progress.finish()
        #utils.clear_progressbar()

        print('[%02d] rec loss: %.5f | kld loss: %.5f (%d)' % (epoch, epoch_rec/opt.epoch_size, epoch_kld/opt.epoch_size, epoch*opt.epoch_size*opt.batch_size))

        # plot some stuff
        hsvg_net.eval()
        with torch.no_grad():
            x = next(testing_batch_generator)
            plot(hsvg_net, x, epoch)
            plot_rec(hsvg_net, x, epoch)

        # save the model
        if (epoch + 1)%10 == 0:
            file_path = '{}/hsvgnet_ep{:04d}.pth.tar'.format(checkpoint_dir, epoch)
            torch.save({
                'epoch': epoch,
                'hsvg_net': hsvg_net.state_dict(),
                'hsvg_optimizer': hsvg_optimizer.state_dict()},
                file_path)
            print('{} was saved.'.format(file_path))

    train_writer.export_scalars_to_json(checkpoint_dir + "/train_log/train_summery.json")
    train_writer.close()

