import torch
from torch.nn.utils.rnn import pack_padded_sequence, pad_sequence
from customDatasetMakers import preprocess_data, ian_dataset
from customModels import IanRNN, IanMLP, HiroLinear

from dataSettings import nx, train_shots, val_shots, test_shots, val_indices

import configparser
import os
import sys
import time

models={'IanRNN': IanRNN, 'IanMLP': IanMLP, 'HiroLinear': HiroLinear}

if (len(sys.argv)-1) > 0:
    config_filename=sys.argv[1]
else:
    config_filename='configs/default.cfg'

config=configparser.ConfigParser()
config.read(config_filename)
preprocessed_data_filenamebase=config['preprocess']['preprocessed_data_filenamebase']
model_type=config['model']['model_type']
bucket_size=config['optimization'].getint('bucket_size')
n_epochs=config['optimization'].getint('n_epochs')
lr=config['optimization'].getfloat('lr')
lr_gamma=config['optimization'].getfloat('lr_gamma')
lr_stop_epoch=config['optimization'].getint('lr_stop_epoch')
early_saving=config['optimization'].getboolean('early_saving')
l1_lambda=config['optimization'].getfloat('l1_lambda')
l2_lambda=config['optimization'].getfloat('l2_lambda')
profiles=config['inputs']['profiles'].split()
actuators=config['inputs']['actuators'].split()
parameters=config['inputs']['parameters'].split()
autoregression_num_steps=int(config['optimization'].get('autoregression_num_steps',1))
if autoregression_num_steps<1:
    autoregression_num_steps=1
autoregression_start_epoch=int(n_epochs/2)

model_hyperparams={key: int(val) for key,val in dict(config[model_type]).items()}

# dump to same location as the config filename, with .tar instead of .cfg
output_filename=os.path.join(config['model']['output_dir'],config['model']['output_filename_base']+".tar")

print('Organizing train data from preprocessed_data')
start_time=time.time()
x_train, y_train, shots, times = ian_dataset(preprocessed_data_filenamebase+'train.pkl',
                                             profiles, actuators, parameters,
                                             sort_by_size=True)
print(f'...took {(time.time()-start_time):0.2f}s')
print('Organizing validation data from preprocessed_data')
start_time=time.time()
x_val, y_val, shots, times = ian_dataset(preprocessed_data_filenamebase+'val.pkl',
                                         profiles, actuators, parameters,
                                         sort_by_size=True)
print(f'...took {(time.time()-start_time):0.2f}s')

state_length=len(profiles)*33+len(parameters)
actuator_length=len(actuators)
model=models[model_type](input_dim=state_length+2*actuator_length, output_dim=state_length,
                         **model_hyperparams)

def masked_loss(loss_fn,
                output, target,
                lengths):
    mask = torch.zeros(len(lengths), max(lengths))
    for i, length in enumerate(lengths):
        mask[i, :length]=1
    mask=mask.to(output.device)
    output=output*mask[..., None]
    target=target*mask[..., None]
    # normalize by dividing out true number of time samples in all batches
    # times the state size
    return loss_fn(output, target) / (sum(lengths)*output.size(-1))

# I divide out by myself since different sequences/batches have different sizes
loss_fn=torch.nn.MSELoss(reduction='sum')

train_losses=[]
val_losses=[]
optimizer = torch.optim.Adam(model.parameters(), lr=lr, weight_decay=1e-5)
#scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones=[10,30,50,70], gamma=lr_gamma, verbose=True)
#scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer, lr_gamma, last_epoch=lr_stop_epoch)

print('Training...')
if torch.cuda.is_available():
    device='cuda'
    if torch.cuda.device_count() > 1:
        model = torch.nn.DataParallel(model)
    print(f"Using {torch.cuda.device_count()} GPU(s)")
else:
    device = 'cpu'
    print("Using CPU")
model.to(device)
param_size = 0
for param in model.parameters():
    param_size += param.nelement() * param.element_size()
buffer_size = 0
for buffer in model.buffers():
    buffer_size += buffer.nelement() * buffer.element_size()
size_all_mb = (param_size + buffer_size) / 1024**2
print('model size: {:.3f}MB'.format(size_all_mb))
start_time=time.time()
prev_time=start_time

# make buckets of near-even size from a sorted array of arrays
def make_bucket(arrays, bucket_size):
    buckets=[]
    current_bucket=[]
    current_len=0
    for arr in arrays:
        arr_len=len(arr)
        current_bucket.append(arr)
        current_len+=arr_len
        if current_len > bucket_size:
            buckets.append(current_bucket)
            current_bucket=[]
            current_len=0
    if len(current_bucket)>0:
        buckets.append(current_bucket)
    return buckets

train_x_buckets = make_bucket(x_train, bucket_size)
train_y_buckets = make_bucket(y_train, bucket_size)
train_length_buckets = [[len(arr) for arr in bucket] for bucket in train_x_buckets]

val_x_buckets = make_bucket(x_val, bucket_size)
val_y_buckets = make_bucket(y_val, bucket_size)
val_length_buckets = [[len(arr) for arr in bucket] for bucket in val_x_buckets]

avg_train_losses=[]
avg_val_losses=[]
for epoch in range(n_epochs):
    if epoch<=autoregression_start_epoch:
        reset_probability=1
    else:
        avg_steps_slope=(1-autoregression_num_steps)/(n_epochs-autoregression_start_epoch)
        avg_steps=avg_steps_slope*(n_epochs-(epoch+1))+autoregression_num_steps
        reset_probability=1./avg_steps
        print(f'Autoregression on, average timestep {avg_steps:0.1f}')
    model.train()
    train_losses=[]
    for which_bucket in torch.randperm(len(train_x_buckets)):
        random_order=torch.randperm(len(train_x_buckets[which_bucket]))
        x_bucket=[train_x_buckets[which_bucket][i] for i in random_order]
        y_bucket=[train_y_buckets[which_bucket][i] for i in random_order]
        length_bucket=[train_length_buckets[which_bucket][i] for i in random_order]

        padded_x=pad_sequence(x_bucket, batch_first=True)
        padded_y=pad_sequence(y_bucket, batch_first=True)
        padded_x=padded_x.to(device)
        padded_y=padded_y.to(device)

        optimizer.zero_grad()
        model_output=model(padded_x,reset_probability=reset_probability)
        train_loss=masked_loss(loss_fn,
                               model_output, padded_y,
                               length_bucket)
        # L1 regularization
        '''l1_reg = torch.tensor(0.0, device=device)
        for param in model.parameters():
            l1_reg += torch.abs(param).sum()
        train_loss += l1_lambda*l1_reg # lambda is the hyperparameter defined in cfg

        # L2 regularization
        l2_reg = torch.tensor(0.0, device=device)
        for param in model.parameters():
            l2_reg += torch.norm(param, p=2).sum()
        train_loss += l2_lambda * l2_reg'''

        # Backpropagation
        train_loss.backward()
        optimizer.step()
        train_losses.append(train_loss.item())
    #scheduler.step()
    avg_train_losses.append(sum(train_losses)/len(train_losses)) # now divide by total number of samples to get mean over steps/batches
    model.eval()
    val_losses=[]
    with torch.no_grad():
        for which_bucket in range(len(val_x_buckets)):
            x_bucket=val_x_buckets[which_bucket]
            y_bucket=val_y_buckets[which_bucket]
            length_bucket=val_length_buckets[which_bucket]
            padded_x=pad_sequence(x_bucket, batch_first=True)
            padded_y=pad_sequence(y_bucket, batch_first=True)
            padded_x=padded_x.to(device)
            padded_y=padded_y.to(device)
            model_output = model(padded_x,reset_probability=reset_probability)
            val_loss = masked_loss(loss_fn,
                                   model_output, padded_y,
                                   length_bucket)
            val_losses.append(val_loss.item())
        avg_val_losses.append(sum(val_losses)/len(val_losses))
    print(f'{epoch+1:4d}/{n_epochs}({(time.time()-prev_time):0.2f}s)... train: {avg_train_losses[-1]:0.2e}, val: {avg_val_losses[-1]:0.2e};')
    if (not early_saving) or avg_val_losses[-1]==min(avg_val_losses):
        print(f"Checkpoint")
        torch.save({
            'epoch': epoch,
            'val_indices': val_indices,
            'model_state_dict': model.state_dict(),
            'optimizer_state_dict': optimizer.state_dict(),
            #'scheduler_state_dict': scheduler.state_dict(),
            'train_losses': avg_train_losses,
            'val_losses': avg_val_losses,
            'profiles': profiles,
            'actuators': actuators,
            'parameters': parameters,
            'model_hyperparams': model_hyperparams,
            #'space_inds': space_inds,
            'exclude_ech': True
        }, output_filename)
    prev_time=time.time()

print(f'...took {(time.time()-start_time)/60:0.2f}min')
