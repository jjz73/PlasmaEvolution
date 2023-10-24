import numpy as np
import scipy.constants as sc
import copy
# number of x points in profile data
nx=33
# timestep in dataset, in seconds
DT=0.02
use_gyroBohm = False
# No normalization for qpsi! Instead, code normalizes/denormalizes w/ inverse
#   i.e. by transforming to iota = 1/q (mean & std for q would be ignored)
normalizations={
    'zipfit_etempfit_rho': {'mean': 0, 'std': 1},
    'zipfit_edensfit_rho': {'mean': 0, 'std': 1},
    'neped_joe': {'mean': 0, 'std': 1},
    'zipfit_trotfit_rho': {'mean': 0, 'std': 1e2},
    'zipfit_itempfit_rho': {'mean': 0, 'std': 1},
    'pres_EFIT01': {'mean': 0, 'std': 1e4},
    'pinj': {'mean': 0, 'std': 1e3},
    'tinj': {'mean': 0, 'std': 1},
    'ipsiptargt': {'mean': 0, 'std': 1},
    'ip': {'mean': 0, 'std': 1e6},
    'bt': {'mean': 0, 'std': 1},
    'dstdenp': {'mean': 0, 'std': 1},
    'gasA': {'mean': 0, 'std': 1},
    'gasB': {'mean': 0, 'std': 1},
    'gasC': {'mean': 0, 'std': 1},
    'gasD': {'mean': 0, 'std': 1},
    'li_EFIT01': {'mean': 0, 'std': 1},
    'tribot_EFIT01': {'mean': 0, 'std': 1},
    'tritop_EFIT01': {'mean': 0, 'std': 1},
    'aminor_EFIT01': {'mean': 0, 'std': 1},
    'rmaxis_EFIT01': {'mean': 0, 'std': 1},
    'dssdenest': {'mean': 0, 'std': 1},
    'kappa_EFIT01': {'mean': 0, 'std': 1},
    'volume_EFIT01': {'mean': 0, 'std': 10},
    'betan_EFIT01': {'mean': 0, 'std': 1},
    'epedHeight': {'mean': 0, 'std': 5e-3},
    'eped_te_prediction': {'mean': 0, 'std': 1},
    'epedHeightForNe1': {'mean': 0, 'std': 5e-3},
    'epedHeightForNe3': {'mean': 0, 'std': 5e-3},
    'epedHeightForNe5': {'mean': 0, 'std': 5e-3},
    'epedHeightForNe7': {'mean': 0, 'std': 5e-3}
    }
if use_gyroBohm:
    normalizations['zipfit_edensfit_rho'] = {'mean': 0, 'std': 1}
    normalizations['zipfit_etempfit_rho'] = {'mean': 0, 'std': 1}
    normalizations['zipfit_itempfit_rho'] = {'mean': 0, 'std': 1}
# if average normalized data for shot greater than this many deviations away,
# exclude the shot from the dataset
deviation_cutoff=10

#min_shot=170000
min_shot=140888
#max_shot=171000
max_shot=200000
val_indices=[np.random.randint(1,10)]
test_indices=[0]

train_shots=[shot for shot in range(min_shot,max_shot) if shot%10 not in val_indices+test_indices]
val_shots=[shot for shot in range(min_shot,max_shot) if shot%10 in val_indices]
test_shots=[shot for shot in range(min_shot,max_shot) if shot%10 in test_indices]

#train_shots=[163303, 170047, 170048]
#val_shots=[]
#test_shots=[]

# ohmic power in Watts, to add to Pinj to get power for taue calculation
ohmicPower=5e5
# min and max taue in seconds
taueMin=0.010
taueMax=1.000

IMPURITY_FRACTION=0.04
# or from
#Zeff=2 flat profile
#Zimp=6
#IMPURITY_FRACTION=(Zeff-1)/(Zimp*(Zimp-Zeff)) #=1/24~4%
IMPURITY_Z=6
KEV_PER_1019_TO_J=1.602e3

# actuators is [] since the output state only has profiles and parameters,
# but the input state has actuators at t and t+1 also
def state_to_dic(state_arrs, profiles, parameters, actuators=[]):
    #state_arrs=torch.atleast_2d(state_arrs)
    dic={sig: None for sig in profiles+parameters}
    ind,next_ind=0,0
    for profile in profiles:
        next_ind=ind+nx
        dic[profile]=[state_arr[ind:next_ind] for state_arr in state_arrs]
        ind=next_ind
    for parameter in parameters:
        dic[parameter]=[state_arr[ind] for state_arr in state_arrs]
        ind=ind+1
    for actuator in actuators:
        dic[actuator]=[state_arr[ind] for state_arr in state_arrs]
        ind=ind+1
    # in future could also return the next step values for actuators
    return dic

def get_gyro_normalized_dic(input_dic):
    output_dic = copy.copy(input_dic)
    Te = input_dic['zipfit_etempfit_rho']
    Ti = np.clip(input_dic['zipfit_itempfit_rho'], 0.01, None) # keeps Ti above 0.01
    ne = input_dic['zipfit_edensfit_rho']
    Bt = np.abs(np.repeat(input_dic['bt'][:,:,np.newaxis], len(Te[0][0]), axis=2)) # make Bt flat radial profile
    a = np.repeat(input_dic['aminor_EFIT01'][:,:,np.newaxis], len(Te[0][0]), axis=2) # make a flat radial profile
    temp_frac = Te/Ti
    rho_star = np.sqrt(Ti) / (a * Bt)
    beta = ne * Te / Bt**2
    output_dic['zipfit_etempfit_rho']=temp_frac
    output_dic['zipfit_itempfit_rho']=rho_star
    output_dic['zipfit_edensfit_rho']=beta
    return output_dic

def get_gyro_denormalized_dic(input_dic):
    output_dic = copy.copy(input_dic)
    temp_frac = input_dic['zipfit_etempfit_rho']
    rho_star = input_dic['zipfit_itempfit_rho']
    beta = input_dic['zipfit_edensfit_rho']
    Bt = np.repeat(input_dic['bt'][:,:,np.newaxis], len(beta[0][0]), axis=2)
    a = np.repeat(input_dic['aminor_EFIT01'][:,:,np.newaxis], len(beta[0][0]), axis=2)
    Te = temp_frac * Ti
    Ti = (a * Bt * rho_star)**2
    ne = beta * Bt**2 / Te
    output_dic['zipfit_etempfit_rho']=Te
    output_dic['zipfit_itempfit_rho']=Ti
    output_dic['zipfit_edensfit_rho']=ne
    return output_dic

# excluded_sigs for e.g. shotnum and times from preprocessed data
def get_normalized_dic(denormed_dic, excluded_sigs=[]):
    if use_gyroBohm:
        denormed_dic=get_gyro_normalized_dic(denormed_dic)
    normalized_dic={}
    for sig in denormed_dic:
        if sig not in excluded_sigs:
            if 'qpsi' in sig:
                normalized_dic[sig] = 1. / denormed_dic[sig]
            else:
                normalized_dic[sig] = (denormed_dic[sig] - normalizations[sig]['mean']) / normalizations[sig]['std']
        else:
            normalized_dic[sig] = denormed_dic[sig]
    return normalized_dic

def get_denormalized_dic(normed_dic, excluded_sigs=[]):
    denormalized_dic={}
    for sig in normed_dic:
        if sig not in excluded_sigs:
            if 'qpsi' in sig:
                denormalized_dic[sig] = 1. / normed_dic[sig]
            else:
                denormalized_dic[sig] = (normed_dic[sig] * normalizations[sig]['std']) + normalizations[sig]['mean']
        else:
            denormalized_dic[sig] = normed_dic[sig]
    if use_gyroBohm:
        denormalized_dic=get_gyro_denormalized_dic(denormalized_dic)
    return denormalized_dic
