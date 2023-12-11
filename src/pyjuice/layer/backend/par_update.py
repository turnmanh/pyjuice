from __future__ import annotations

import torch
import torch.nn as nn
import triton
import triton.language as tl
from numba import njit

from pyjuice.nodes import CircuitNodes


@njit
def _record_par_blks(par_start_ids, pflow_start_ids, blk_sizes, blk_intervals, global_nids, 
                     num_edges_per_ng, ns_num_node_groups, ns_group_size, cs_group_size, pid, 
                     global_nid, par_start, pflow_start, BLOCK_SIZE):
    for local_ngid in range(ns_num_node_groups):
        num_edges = num_edges_per_ng[local_ngid]
        num_chs = num_edges * cs_group_size

        for sid in range(0, num_chs, BLOCK_SIZE):
            eid = min(sid + BLOCK_SIZE, num_chs)
            blk_size = eid - sid

            for gid in range(ns_group_size):
                psid = par_start + sid * ns_group_size + gid
                pfsid = pflow_start + sid * ns_group_size + gid
                global_ind = global_nid + gid

                par_start_ids[pid] = par_start + sid * ns_group_size + gid
                pflow_start_ids[pid] = pflow_start + sid * ns_group_size + gid
                blk_sizes[pid] = blk_size
                blk_intervals[pid] = ns_group_size
                global_nids[pid] = global_nid + gid

                pid += 1

        global_nid += ns_group_size

    return global_nid, pid


@torch.no_grad()
def compile_par_update_fn(root_ns: CircuitNodes, BLOCK_SIZE: int = 32, buffer_inc_interval: int = 10000, use_numba: bool = True):

    assert BLOCK_SIZE & (BLOCK_SIZE - 1) == 0, "`BLOCK_SIZE` must be power of 2."

    par_start_ids = np.zeros([buffer_inc_interval], dtype = np.int64)
    pflow_start_ids = np.zeros([buffer_inc_interval], dtype = np.int64)
    blk_sizes = np.zeros([buffer_inc_interval], dtype = np.int64)
    blk_intervals = np.zeros([buffer_inc_interval], dtype = np.int64)
    global_nids = np.zeros([buffer_inc_interval], dtype = np.int64)
    pid = 0

    global_nid = 0
    for ns in root_ns:
        if not ns.is_sum() or ns.is_tied():
            continue

        par_start = ns._param_range[0]
        pflow_start = ns._param_flow_range[0]
        tot_n_pars = ns._param_range[1] - ns._param_range[0]

        num_edges_per_ng = torch.bincount(ns.edge_ids[0,:], minlength = ns.num_node_groups).contiguous().numpy()

        # Enlarge the buffer if needed
        est_num_slots = triton.cdiv(ns.edges.size(1) * ns.group_size * ns.ch_group_size, BLOCK_SIZE) + ns.num_nodes
        if pid + est_num_slots > par_start_ids.shape[0]:
            curr_size = par_start_ids.shape[0]
            inc_shape = triton.cdiv(pid + est_num_slots - curr_size, buffer_inc_interval) * buffer_inc_interval

            par_start_ids = np.ascontiguousarray(par_start_ids.resize(curr_size + inc_shape))
            pflow_start_ids = np.ascontiguousarray(pflow_start_ids.resize(curr_size + inc_shape))
            blk_sizes = np.ascontiguousarray(blk_sizes.resize(curr_size + inc_shape))
            blk_intervals = np.ascontiguousarray(blk_intervals.resize(curr_size + inc_shape))
            global_nids = np.ascontiguousarray(global_nids.resize(curr_size + inc_shape))

        if use_numba:

            ns_num_node_groups = ns.num_node_groups
            ns_group_size = ns.group_size
            cs_group_size = ns.ch_group_size

            global_nid, pid = _record_par_blks(
                par_start_ids, pflow_start_ids, blk_sizes, blk_intervals, global_nids, 
                num_edges_per_ng, ns_num_node_groups, ns_group_size, cs_group_size, pid, 
                global_nid, par_start, pflow_start, BLOCK_SIZE
            )

        else:
            ns_gid_range = torch.arange(0, ns.group_size)

            for local_ngid in range(ns.num_node_groups):
                num_edges = num_edges_per_ng[local_ngid]
                num_chs = num_edges * ns.ch_group_size

                for sid in range(0, num_chs, BLOCK_SIZE):
                    eid = min(sid + BLOCK_SIZE, num_chs)
                    blk_size = eid - sid

                    curr_psids = par_start + sid * ns.group_size + ns_gid_range
                    curr_pfsids = pflow_start + sid * ns.group_size + ns_gid_range
                    curr_global_nids = global_nid + ns_gid_range

                    par_start_ids[pid:pid+ns.group_size] = curr_psids
                    pflow_start_ids[pid:pid+ns.group_size] = curr_pfsids
                    blk_sizes[pid:pid+ns.group_size] = blk_size
                    blk_intervals[pid:pid+ns.group_size] = ns.group_size
                    global_nids[pid:pid+ns.group_size] = curr_global_nids

                    pid += ns.group_size

                global_nid += ns.group_size

    par_start_ids = torch.from_numpy(par_start_ids[:pid]).contiguous()
    pflow_start_ids = torch.from_numpy(pflow_start_ids[:pid]).contiguous()
    blk_sizes = torch.from_numpy(blk_sizes[:pid]).contiguous()
    blk_intervals = torch.from_numpy(blk_intervals[:pid]).contiguous()
    global_nids = torch.from_numpy(global_nids[:pid]).contiguous()

    cum_pflows = torch.zeros([global_nids[-1] + 1], dtype = torch.float32)
    
    metadata = {"tot_num_nodes": global_nids[-1] + 1, "BLOCK_SIZE": BLOCK_SIZE}

    return par_start_ids, pflow_start_ids, blk_sizes, blk_intervals, global_nids, cum_pflows, metadata


@triton.jit
def cum_pflow_kernel(cum_pflows, param_flows, pflow_start_ids, blk_sizes, blk_intervals, 
                     global_nids, num_blocks, BLOCK_ID: tl.constexpr, BLOCK_SIZE: tl.constexpr):

    pid = tl.program_id(axis = 0)

    offs_m = pid * BLOCK_ID + tl.arange(0, BLOCK_ID)
    mask_m = offs_m < num_blocks

    offs_blk = tl.arange(0, BLOCK_SIZE)

    pflow_start = tl.load(pflow_start_ids + offs_m, mask = mask_m, other = 0)
    blk_size = tl.load(blk_sizes + offs_m, mask = mask_m, other = 0)
    blk_interval = tl.load(blk_intervals + offs_m, mask = mask_m, other = 0)
    global_nid = tl.load(global_nids + offs_m, mask = mask_m, other = 0)

    offs_pflow = pflow_start[:,None] + offs_blk[None,:] * blk_interval[:,None]
    mask_pflow = mask_m[:,None] & (offs_blk[None,:] < blk_size[:,None])
    pflows = tl.load(param_flows + offs_pflow, mask = mask_pflow, other = 0)
    nflows = tl.sum(pflows, axis = 1)

    tl.atomic_add(cum_pflows + global_nid, nflows, mask = mask_m)


def par_update_kernel(params, param_flows, cum_pflows, nchs, par_start_ids, pflow_start_ids, blk_sizes, blk_intervals,
                      global_nids, constexprs, num_blocks, BLOCK_ID: tl.constexpr, BLOCK_SIZE: tl.constexpr):

    pid = tl.program_id(axis = 0)

    # Retrieve the constants
    step_size = tl.load(constexprs)
    pseudocount = tl.load(constexprs + 1)

    offs_m = pid * BLOCK_ID + tl.arange(0, BLOCK_ID)
    mask_m = offs_m < num_blocks

    offs_blk = tl.arange(0, BLOCK_SIZE)

    par_start = tl.load(par_start_ids + offs_m, mask = mask_m, other = 0)
    pflow_start = tl.load(pflow_start_ids + offs_m, mask = mask_m, other = 0)
    blk_size = tl.load(blk_sizes + offs_m, mask = mask_m, other = 0)
    blk_interval = tl.load(blk_intervals + offs_m, mask = mask_m, other = 0)
    global_nid = tl.load(global_nids + offs_m, mask = mask_m, other = 0)

    offs_pflow = pflow_start[:,None] + offs_blk[None,:] * blk_interval[:,None]
    mask_pflow = mask_m[:,None] & (offs_blk[None,:] < blk_size[:,None])
    pflows = tl.load(param_flows + offs_pflow, mask = mask_pflow, other = 0)

    nflows = tl.load(cum_pflows + global_nid, mask = mask_m, other = 1)
    nch = tl.load(nchs + global_nid, mask = mask_m, other = 1)

    new_param = (pflows + pseudocount / nch[:,None]) / (nflows[:,None] + pseudocount)

    offs_par = par_start[:,None] + offs_blk[None,:] * blk_interval[:,None]
    old_param = tl.load(params + offs_par, mask = mask_pflow, other = 0)

    updated_param = (1.0 - step_size) * old_param + step_size * new_param
    tl.store(params + offs_par, updated_param, mask = mask_pflow)


def em_par_update(params, param_flows, par_start_ids, pflow_start_ids, blk_sizes, blk_intervals, 
                  global_nids, metadata, step_size: float, pseudocount: float = 0.0, cum_pflows = None):

    tot_num_nodes = metadata["tot_num_nodes"]
    BLOCK_SIZE = metadata["BLOCK_SIZE"]

    if cum_pflows is None:
        cum_pflows = torch.zeros([tot_num_nodes], dtype = torch.float32, device = params.device)
    else:
        cum_pflows[:] = 0.0

    num_blocks = par_start_ids.size(0)
    BLOCK_ID = 2048 // BLOCK_SIZE

    grid = (triton.cdiv(num_blocks, BLOCK_ID),)

    cum_pflow_kernel[grid](
        cum_pflows, param_flows, pflow_start_ids, blk_sizes, blk_intervals, 
        global_nids, num_blocks, BLOCK_ID, BLOCK_SIZE
    )

    constexprs = torch.tensor([step_size, pseudocount]).to(params.device)

    par_update_kernel[grid](
        params, param_flows, cum_pflows, par_start_ids, pflow_start_ids, blk_sizes, blk_intervals,
        global_nids, constexprs, num_blocks, BLOCK_ID, BLOCK_SIZE
    )
