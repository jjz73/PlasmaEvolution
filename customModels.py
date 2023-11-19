from dataSettings import nx
import copy
import torch
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence

class IanMLP(torch.nn.Module):
    def __init__(self, input_dim, output_dim,
                 hidden_dim=100, extra_layers=1):
        super().__init__()
        self.mlp=torch.nn.Sequential()
        self.mlp.append(torch.nn.Linear(input_dim, hidden_dim))
        self.mlp.append(torch.nn.ReLU())
        for i in range(extra_layers):
            self.mlp.append(torch.nn.Linear(hidden_dim, hidden_dim))
            self.mlp.append(torch.nn.ReLU())
        self.mlp.append(torch.nn.Linear(hidden_dim, output_dim))
    def forward(self, padded_input):
        return self.mlp(padded_input)

class IanRNN(torch.nn.Module):
    def __init__(self, input_dim, output_dim,
                 encoder_dim=100, encoder_extra_layers=1,
                 rnn_dim=100, rnn_num_layers=1,
                 decoder_dim=100, decoder_extra_layers=1,
                 rnn_type='lstm'
                 ):
        super().__init__()
        self.encoder = torch.nn.Sequential()
        self.encoder.append(torch.nn.Linear(input_dim, encoder_dim))
        self.encoder.append(torch.nn.ReLU())
        for i in range(encoder_extra_layers):
            self.encoder.append(torch.nn.Linear(encoder_dim, encoder_dim))
            self.encoder.append(torch.nn.ReLU())
        # batch_size x time_length x input_dim
        self.rnn_type=rnn_type
        if self.rnn_type=='lstm':
            self.rnn=torch.nn.LSTM(
                encoder_dim, rnn_dim,
                batch_first=True
            )
        elif self.rnn_type=='linear':
            self.rnn=torch.nn.Linear(encoder_dim, rnn_dim)
        self.decoder = torch.nn.Sequential()
        self.decoder.append(torch.nn.Linear(rnn_dim, decoder_dim))
        self.decoder.append(torch.nn.ReLU())
        for i in range(decoder_extra_layers):
            self.decoder.append(torch.nn.Linear(decoder_dim, decoder_dim))
            self.decoder.append(torch.nn.ReLU())
        self.decoder.append(torch.nn.Linear(decoder_dim, output_dim))
        self.rnn_num_layers=rnn_num_layers
        self.rnn_dim=rnn_dim
        self.output_dim=output_dim
    # reset_probability is the probability we use the true input
    # rather than autoregressed input for the next step
    # nwarmup is number of steps for which it won't autoregress
    # padded_input is like (nsamples, ntimes, nstates)
    def forward(self, padded_input, reset_probability=0, nwarmup=0):
        # inference without autoregression (20x faster)
        if reset_probability>=1:
            embedding=self.encoder(padded_input)
            if self.rnn_type=='lstm':
                embedding_evolved,_=self.rnn(embedding)
            else:
                embedding_evolved=self.rnn(embedding)
            padded_output=self.decoder(embedding_evolved)
        # inference with probabilistic autoregression
        else:
            # number of times
            seq_len=padded_input.size()[-2]
            # padded_output dim is padded_input without actuator chunk
            padded_output=torch.zeros(padded_input[:,:,:self.output_dim].size())
            # maintain previous output for autoregression (start at true t=0 state)
            prev_output=padded_input[:,0,:self.output_dim].unsqueeze(1)
            for t_ind in range(seq_len):
                if (t_ind<=nwarmup) or (torch.rand(1).item() < reset_probability):
                    # predict from true state (don't autoregress this timestep)
                    this_input=padded_input[:,t_ind,:].unsqueeze(1)
                else:
                    # autoregress: use previous output with actuators
                    actuator_array=padded_input[:,t_ind,self.output_dim:].unsqueeze(1)
                    this_input=torch.cat((prev_output,actuator_array),dim=-1)
                ####### EVOLVE THE STATE
                embedding=self.encoder(this_input)
                # note hidden state has both state and memory, (h,c)
                # on first timestep initialize hidden state to 0 by not passing it in
                if self.rnn_type=='lstm':
                    if t_ind==0:
                        embedding_evolved,hidden_state=self.rnn(embedding)
                    else:
                        embedding_evolved,hidden_state=self.rnn(embedding,hidden_state)
                else:
                    embedding_evolved=self.rnn(embedding)
                this_output=self.decoder(embedding_evolved)
                ####### SAVE THE OUTPUT
                prev_output = this_output
                padded_output[:,t_ind,:] = prev_output.squeeze(1)
        return padded_output

class InverseLeakyReLU(torch.nn.Module):
    def __init__(self, slope=0.01):
        super(InverseLeakyReLU, self).__init__()
        self.slope = slope

    def forward(self, x):
        return torch.where(x < 0, x / self.slope, x)

class HiroLinear(torch.nn.Module):
    def __init__(self, input_dim, output_dim,
                 encoder_extra_layers=1,
                 decoder_extra_layers=1
                 ):
        super().__init__()

        self.input_dim = input_dim
        self.output_dim = output_dim
        state_dim = output_dim

        self.encoder = torch.nn.Sequential()
        self.encoding_first_layer = torch.nn.Sequential(
            torch.nn.Linear(state_dim, state_dim),
            torch.nn.LeakyReLU(negative_slope=0.01))
        self.encoder.append(self.encoding_first_layer)
        self.encoding_extra_layers = torch.nn.Sequential()
        for i in range(encoder_extra_layers):
            self.encoding_extra_layers.append(torch.nn.Linear(state_dim, state_dim))
            self.encoding_extra_layers.append(torch.nn.LeakyReLU(negative_slope=0.01))
        self.encoder.append(self.encoding_extra_layers)

        # linear A and B matrices
        self.A = torch.nn.Linear(state_dim, state_dim)
        actuator_length = (input_dim - state_dim) // 2 # divide by 2 cuz input has u_t and u_t+1
        self.B = torch.nn.Linear(actuator_length, state_dim)

        self.decoder = torch.nn.Sequential()
        self.decoding_first_layer = torch.nn.Sequential(
            InverseLeakyReLU(slope=0.01),
            torch.nn.Linear(state_dim, state_dim))
        self.decoder.append(self.decoding_first_layer)
        self.decoding_extra_layers = torch.nn.Sequential()
        for i in range(decoder_extra_layers):
            self.decoding_extra_layers.append(InverseLeakyReLU(slope=0.01))
            self.decoding_extra_layers.append(torch.nn.Linear(state_dim, state_dim))
        self.decoder.append(self.decoding_extra_layers)
    def forward(self, padded_input, reset_probability=0, nwarmup=0):
        state_dim = self.output_dim
        x_t = padded_input[:, :, :state_dim]
        actuator_length = (self.input_dim - state_dim) // 2 # divide by 2 cuz input has u_t and u_t+1
        u_t = padded_input[:, :, state_dim:state_dim+actuator_length]
        u_t1 = padded_input[:, :, state_dim+actuator_length:]
        z_t = self.encoder(x_t)
        # inference without autoregression (20x faster)
        if reset_probability>=1:
            z_t1=self.A(z_t) + self.B(u_t)
        # inference with probabilistic autoregression
        else:
            # number of times
            seq_len=padded_input.size()[-2]
            # padded_output dim is padded_input without actuator chunk
            z_t1=torch.zeros(z_t.size())
            # maintain previous output for autoregression (start at true t=0 state)
            prev_output=z_t[:,0,:].unsqueeze(1)
            for t_ind in range(seq_len):
                if (t_ind<=nwarmup) or (torch.rand(1).item() < reset_probability):
                    # predict from true state (don't autoregress this timestep)
                    this_input = z_t[:, t_ind, :].unsqueeze(1)
                else:
                    # autoregress: use previous output with actuators
                    this_input=prev_output
                ####### EVOLVE THE STATE
                this_output=self.A(this_input) + self.B(u_t[:, t_ind, :].unsqueeze(1)) # is this the right actuators?
                ####### SAVE THE OUTPUT
                prev_output = this_output
                z_t1[:,t_ind,:] = prev_output.squeeze(1)
        x_t1 = self.decoder(z_t1)
        return x_t1

# simple mapping, given just actuators over time try to predict profiles
# I imagine lookback=0 is most sensible
class ProfilesFromActuatorsAdvanced(torch.nn.Module):
    def __init__(self, profiles, actuators):
        super().__init__()
        self.nprofiles = len(profiles)
        self.nactuators = len(actuators)
        self.c = torch.nn.Parameter(torch.randn((1,self.nprofiles*nx,self.nactuators),
                                                requires_grad=True, dtype=torch.float))
    def forward(self, input_profiles, input_actuators, input_parameters):
        # Computes the outputs / predictions
        # batch_size, nx*len(profiles), lookahead
        this_batch_size=input_actuators.shape[0]
        pseudo_profiles_over_time = torch.bmm(self.c.repeat(this_batch_size,1,1),
                                              input_actuators)
        # sum over the lookahead
        pseudo_profiles = pseudo_profiles_over_time.sum(dim=-1).reshape(this_batch_size,self.nprofiles,nx)
        return pseudo_profiles

# reproducing 2021 paper
# dataset should be built w/ just last timestep as output
class PlasmaConv2D(torch.nn.Module):
    def __init__(self, profiles, actuators, parameters):
        super().__init__()
        self.conv = torch.nn.Sequential(
            torch.nn.Conv1d(len(profiles),10,2,padding='same'),
            torch.nn.Conv1d(10,20,4,padding='same'),
            torch.nn.Conv1d(20,40,6,padding='same'),
            torch.nn.Conv1d(40,80,8,padding='same'),
            torch.nn.ReLU()
        )
        self.actuatorPreRNN = torch.nn.Sequential(
            torch.nn.Linear(len(actuators),10),
            torch.nn.ReLU(),
            torch.nn.Linear(10,20),
            torch.nn.ReLU(),
            torch.nn.Linear(20,40),
            torch.nn.ReLU()
        )
        self.parameterPreRNN = torch.nn.Sequential(
            torch.nn.Linear(len(parameters),10),
            torch.nn.ReLU(),
            torch.nn.Linear(10,20),
            torch.nn.ReLU(),
            torch.nn.Linear(20,40),
            torch.nn.ReLU()
        )
        # remember we'll just take the latest output
        self.actuatorRNN = torch.nn.LSTM(40,80,batch_first=True)
        self.parameterRNN = copy.deepcopy(self.actuatorRNN)
        self.actuatorPostRNN = torch.nn.Sequential(
            torch.nn.Linear(1,8),
            torch.nn.ReLU(),
            torch.nn.Linear(8,16),
            torch.nn.ReLU(),
            torch.nn.Linear(16,33),
            torch.nn.ReLU()
        )
        self.parameterPostRNN = copy.deepcopy(self.actuatorPostRNN)
        self.deconv = torch.nn.Sequential(
            torch.nn.Conv1d(80,40,8,padding='same'),
            torch.nn.Conv1d(40,20,4,padding='same'),
            torch.nn.Conv1d(20,10,4,padding='same'),
            torch.nn.Conv1d(10,len(profiles),2,padding='same'),
            torch.nn.ReLU(),
        )
    def forward(self, profiles_tensor, input_actuators, input_parameters):
        lookahead=input_actuators.shape[1]-input_parameters.shape[1] #present timestep -lookahead-1
        present_profiles=profiles_tensor[:,-lookahead-1,:,:]

        preAddProfiles=self.conv(present_profiles) #input_profiles)
        preAddActuators=self.actuatorPreRNN(input_actuators)
        _, (preAddActuators, _)=self.actuatorRNN(preAddActuators)
        preAddActuators=preAddActuators.permute([1,2,0])
        preAddActuators=self.actuatorPostRNN(preAddActuators)
        preAddParameters=self.parameterPreRNN(input_parameters)
        _, (preAddParameters, _)=self.parameterRNN(preAddParameters)
        preAddParameters=preAddParameters.permute([1,2,0])
        preAddParameters=self.parameterPostRNN(preAddParameters)
        pseudoProfiles=preAddProfiles+preAddActuators+preAddParameters
        outputProfiles=self.deconv(pseudoProfiles)
        return outputProfiles

# simplest RNN possible
# dataset should be built with all timesteps output
class PlasmaGRU(torch.nn.Module):
    def __init__(self, profiles, actuators, parameters):
        super().__init__()
        self.nprofiles=len(profiles)
        self.recurrent = torch.nn.GRU(len(actuators),len(profiles)*nx,batch_first=True)
    def forward(self, profiles_tensor, actuators_tensor, parameters_tensor):
        lookahead=actuators_tensor.shape[1]-parameters_tensor.shape[1] #present timestep -lookahead-1
        present_profile=profiles_tensor[:,-lookahead-1,:,:]
        hiddenProfiles,_=self.recurrent(actuators_tensor[:,-lookahead:,:],
                                        torch.flatten(present_profile,start_dim=1)[None,:])
        outputProfiles=hiddenProfiles.reshape(*hiddenProfiles.shape[:-1],self.nprofiles,nx)
        return outputProfiles

class ProfilesFromActuators(torch.nn.Module):
    def __init__(self, profiles, actuators, nProfilePoints, hidden_size=30):
        super().__init__()
        self.mlp= torch.nn.Sequential(
            torch.nn.Linear(len(actuators), hidden_size),
            torch.nn.Tanh(),
            torch.nn.Linear(hidden_size, hidden_size),
            torch.nn.Tanh(),
            torch.nn.Linear(hidden_size, len(profiles)*nProfilePoints)
        )
    def forward(self, profiles_tensor, actuators_tensor):
        return self.mlp(actuators_tensor)
