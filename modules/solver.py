import os
import time
import numpy as np
import torch
import torch.nn.functional as F
from torch import autocast
from torch.amp import GradScaler
from tqdm import tqdm

from modules.logger.saver import Saver
from modules.logger import utils


# def test(args, model, loss_func, loader_test, saver, vocoder=None):
#     model.eval()

#     # losses
#     test_loss = 0.
    
#     # intialization
#     num_batches = len(loader_test)
#     rtf_all = []
    
#     spk_id_key = 'spk_id'
#     if args.model.use_speaker_embed:
#         spk_id_key = 'spk_embed'
    
#     # run
#     with torch.no_grad():
#         with tqdm(loader_test, desc="test") as pbar:
#             for data in pbar:
#                 fn = data['name'][0].lstrip("data/test/")

#                 # unpack data
#                 for k in data.keys():
#                     if k != 'name':
#                         data[k] = data[k].to(args.device)
                
#                 units = data['units']
                
#                 # forward
#                 st_time = time.time()
#                 signal = model(units, data['f0'], data['volume'], data[spk_id_key])
#                 ed_time = time.time()

#                 # crop
#                 min_len = np.min([signal.shape[1], data['audio'].shape[1]])
#                 signal        = signal[:,:min_len]
#                 data['audio'] = data['audio'][:,:min_len]

#                 # RTF
#                 run_time = ed_time - st_time
#                 song_time = data['audio'].shape[-1] / args.data.sampling_rate
#                 rtf = run_time / song_time
#                 rtf_all.append(rtf)
            
#                 # loss
#                 loss = loss_func(data['audio'], signal)

#                 test_loss += loss.item()

#                 # log
#                 saver.log_audio({fn+'/gt.wav': data['audio'], fn+'/pred.wav': signal})
                
#                 pbar.set_description(fn)
#                 pbar.set_postfix({'loss': loss.item(), 'RTF': rtf})
            
#     # report
#     test_loss /= num_batches
    
#     return test_loss


def train(args, initial_global_step, nets_g, loader_train, loader_test):
    model, optimizer, scheduler = nets_g
    # saver
    saver = Saver(args, initial_global_step=initial_global_step)
    
    last_model_save_step = saver.global_step
    
    expdir_dirname = os.path.split(args.env.expdir)[-1]
    
    # run
    num_batches = len(loader_train)
    model.train()
    
    scaler = GradScaler('cuda')
    if args.train.amp_dtype == 'fp32':
        dtype = torch.float32
    elif args.train.amp_dtype == 'fp16':
        dtype = torch.float16
    elif args.train.amp_dtype == 'bf16':
        dtype = torch.bfloat16
    else:
        raise ValueError(' [x] Unknown amp_dtype: ' + args.train.amp_dtype)
    
    
    # model size
    params_count = utils.get_network_params_amount({'model': model})
    saver.log_info('--- model size ---')
    saver.log_info(params_count)
        
    # TODO: too slow iteration, could be much faster
    saver.log_info('======= start training =======')
    for epoch in range(args.train.epochs):
        for batch_idx, data in enumerate(loader_train):
            saver.global_step_increment()
            optimizer.zero_grad()
            
            # unpack data
            for k in data.keys():
                if k != 'name':
                    data[k] = data[k].to(args.device)
                    
            units = data['units']
            norm_spec = data['norm_spec']
            f0 = data['f0']
                    
            # forward
            if dtype == torch.float32:
                all_signal = model(norm_spec.float())
            else:
                with autocast(device_type=args.device, dtype=dtype):
                    all_signal = model(norm_spec.to(dtype))
                    
            signal, pred_f0 = all_signal[:,:,:-1], all_signal[:,:,-1:]
            
            # optimizer.zero_grad()
            
            losses = []
            
            # minibatch contrastive learning
            # Bring per-frame per-speaker's feature to centroid of top two of different speakers' similarities and itself,
            # keep away per-frame per-speaker's feature to centroid of worst two of different speakers' similarities and itself.
            # These are expecting to learn that different speakers' same utterance mapping to nearly vectors.
            B, frames, T = signal.shape
            for b in range(B):
                # cosine similarity
                opps = [o for o in range(B)
                        if data['spk_id'][b] != data['spk_id'][o]]
                if len(opps) <= 0:
                    # TODO: should be quit backpropagation of epoch itself?
                    continue
                
                last_hop = 1
                # f = 0
                f = torch.randint(0, args.train.frame_hop_random_min-1, (1,))[0]
                while f < frames - 1:
                    ## brute-force
                    opps_sims = F.cosine_similarity(
                        units.float()[b, f:f+1].unsqueeze(0).repeat(len(opps), units.float().shape[1], 1),
                        units.float()[opps, 0:],
                        dim=2)
                    opps_sort_sim_frame = torch.argsort(opps_sims, dim=1)
                    
                    # sim_mini_opp_frames = [units.float()[b, f]]
                    sim_mini_opp_frames = []
                    sim_maxi_opp_frames = [units.float()[b, f]]
                    
                    for i in range(min(len(opps), 2)):  # TODO: parametrize?
                        opps_large_sims = opps_sims[:, opps_sort_sim_frame[-1-i]].diagonal()
                        opps_small_sims = opps_sims[:, opps_sort_sim_frame[i]].diagonal()
                        
                        sim_mini_opp = torch.argmin(opps_small_sims)
                        sim_mini_opp_frame = opps_sort_sim_frame[i][sim_mini_opp]
                        sim_maxi_opp = torch.argmax(opps_large_sims)
                        sim_maxi_opp_frame = opps_sort_sim_frame[-1-i][sim_maxi_opp]
                        
                        sim_maxi_opp_frames.append(units.float()[opps[sim_maxi_opp], sim_maxi_opp_frame])
                        sim_mini_opp_frames.append(units.float()[opps[sim_mini_opp], sim_mini_opp_frame])
                        
                    sim_mini_opps_centroid = torch.mean(torch.stack(sim_mini_opp_frames), dim=0)
                    sim_maxi_opps_centroid = torch.mean(torch.stack(sim_maxi_opp_frames), dim=0)
                    
                    # ## random pick
                    # rand_frame = torch.randint(0, frames, (len(opps),))
                    # opps_sims = F.cosine_similarity(
                    #     units.float()[b, f].repeat(len(opps), 1),
                    #     # units.float()[opps, 0:],
                    #     torch.stack([units[o, i] for o, i in zip(opps, rand_frame)]).float(),
                    #     dim=1)
                    # opps_sort_sim_batch = torch.argsort(opps_sims, dim=0)
                    
                    # # sim_mini_opp_frames = [units.float()[b, f]]
                    # sim_mini_opp_frames = []
                    # sim_maxi_opp_frames = [units.float()[b, f]]
                    
                    # for i in range(min(len(opps), 2)):  # TODO: parametrize?
                    #     sim_maxi_opp = opps_sort_sim_batch[-1-i]
                    #     sim_maxi_opp_frame = rand_frame[sim_maxi_opp]
                    #     sim_mini_opp = opps_sort_sim_batch[i]
                    #     sim_mini_opp_frame = rand_frame[sim_mini_opp]
                        
                    #     sim_maxi_opp_frames.append(units.float()[opps[sim_maxi_opp], sim_maxi_opp_frame])
                    #     sim_mini_opp_frames.append(units.float()[opps[sim_mini_opp], sim_mini_opp_frame])
                        
                    # sim_mini_opps_centroid = torch.mean(torch.stack(sim_mini_opp_frames), dim=0)
                    # sim_maxi_opps_centroid = torch.mean(torch.stack(sim_maxi_opp_frames), dim=0)
                    
                    if dtype == torch.float32:
                        losses.append(
                            F.l1_loss(
                                1. - (F.cosine_similarity(signal[b, f], signal[opps[sim_maxi_opp], sim_maxi_opp_frame], dim=0)*0.5 + 0.5),
                                (1. - (F.cosine_similarity(units.float()[b, f], sim_maxi_opps_centroid, dim=0)*0.5 + 0.5))*args.train.loss_variation)
                        )
                        
                        losses.append(
                            F.l1_loss(
                                F.cosine_similarity(signal[b, f], signal[opps[sim_mini_opp], sim_mini_opp_frame], dim=0)*0.5 + 0.5,
                                (F.cosine_similarity(units.float()[b, f], sim_mini_opps_centroid, dim=0)*0.5 + 0.5)*args.train.low_similar_loss_variation)
                        )
                    else:
                        with autocast(device_type=args.device, dtype=dtype):
                            losses.append(
                                F.l1_loss(
                                    1. - (F.cosine_similarity(signal[b, f], signal[opps[sim_maxi_opp], sim_maxi_opp_frame], dim=0)*0.5 + 0.5),
                                    (1. - (F.cosine_similarity(units[b, f], sim_maxi_opps_centroid, dim=0)*0.5 + 0.5))*args.train.loss_variation)
                            )
                            
                            losses.append(
                                F.l1_loss(
                                    F.cosine_similarity(signal[b, f], signal[opps[sim_mini_opp], sim_mini_opp_frame], dim=0)*0.5 + 0.5,
                                    (F.cosine_similarity(units[b, f], sim_mini_opps_centroid, dim=0)*0.5 + 0.5)*args.train.low_similar_loss_variation)
                            )
                            
                    last_hop = torch.randint(args.train.frame_hop_random_min, args.train.frame_hop_random_max, (1,))[0]
                    # last_hop = 1
                    f += last_hop
                    
            if len(losses) <= 0:
                # TODO: should be quit backpropagation of epoch itself?
                continue
            
            loss = torch.stack([l/(len(losses)/2) for l in losses]).sum()
            
            # calc for the signal should be convergence to normal distribution
            signal_std, signal_mean = signal.std(), signal.mean()
            loss = loss + (F.l1_loss(signal_std, torch.ones_like(signal_std)) + signal_mean.abs().mean())*0.5
            
            # calc loss for pred_f0
            loss = loss*0.5 + F.l1_loss(torch.log2(pred_f0 + 1e-3), torch.log2(f0 + 1e-3))
            
            # handle nan loss
            if torch.isnan(loss):
                raise ValueError(' [x] nan loss ')
            
            # backpropagate
            if dtype == torch.float32:
                loss.backward()
                optimizer.step()
            else:
                scaler.scale(loss).backward()
                scaler.step(optimizer)
                scaler.update()
                
            # log loss
            if saver.global_step % args.train.interval_log == 0:
                saver.log_info(
                    '\repoch: {} | {:3d}/{:3d} | {} | batch/s: {:2.2f} | loss: {:.7f} | lr: {:.6f} | time: {} | step: {}'.format(
                        epoch,
                        batch_idx,
                        num_batches,
                        expdir_dirname,
                        args.train.interval_log/saver.get_interval_time(),
                        loss.item(),
                        scheduler.get_last_lr()[0],
                        saver.get_total_time(),
                        saver.global_step
                    ),
                    end="",
                )
                
                saver.log_value({
                    'train/loss': loss.item(),
                    'train/lr': scheduler.get_last_lr()[0],
                })
                
            # validation
            if saver.global_step % args.train.interval_val == 0:
                optimizer_save = optimizer if args.train.save_opt else None
                
                states = {
                    'scheduler': scheduler.state_dict(),
                    'last_lr': scheduler.get_last_lr(),
                }
                    
                # save latest
                saver.save_model(model, optimizer_save, postfix=f'_{saver.global_step}', states=states)
                
        scheduler.step(loss.item())
                # scheduler.step()
    return

                          
