#!/usr/bin/env python
# Copyright 2014-2018 The PySCF Developers. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Author: Peng Bao <baopeng@iccas.ac.cn>
#

'''
semi-grid Coulomb and eXchange without differencial density matrix

To lower the scaling of coulomb and exchange matrix construction for large system, one 
coordinate is analitical and the other is grid. The traditional two electron 
integrals turn to analytical one electron integrals and numerical integration 
based on grid.(see Friesner, R. A. Chem. Phys. Lett. 1985, 116, 39)

Minimizing numerical errors using overlap fitting correction.(see 
Lzsak, R. et. al. J. Chem. Phys. 2011, 135, 144105)
Grid screening for weighted AO value and DktXkg. 
Two SCF steps: coarse grid then fine grid. There are 5 parameters can be changed:
# threshold for Xg and Fg screening
gthrd = 1e-10
# initial and final grids level
grdlvl_i = 0
grdlvl_f = 1
# norm_ddm threshold for grids change
thrd_nddm = 0.03
# set block size to adapt memory 
sblk = 200

Set mf.direct_scf = False because no traditional 2e integrals
'''

import time
import ctypes
import numpy
import scipy.linalg
from pyscf import lib
from pyscf import gto
from pyscf import dft
from pyscf.lib import logger
from pyscf.df.incore import aux_e2
from pyscf.gto import moleintor
from pyscf.scf import _vhf

from pyscf.gto.moleintor import make_loc

def get_jk_favork(sgx, dm, hermi=1, with_j=True, with_k=True, direct_scf_tol=1e-13):
    t0 = time.clock(), time.time()
    mol = sgx.mol
    grids = sgx.grids
    gthrd = sgx.grids_thrd

    dms = numpy.asarray(dm)
    dm_shape = dms.shape
    nao = dm_shape[-1]
    dms = dms.reshape(-1,nao,nao)
    nset = dms.shape[0]

    if sgx.debug:
        batch_nuc = _gen_batch_nuc(mol)
    else:
        batch_jk = _gen_jk_direct(mol, 's2', with_j, with_k, direct_scf_tol)
    t1 = logger.timer_debug1(mol, "sgX initialziation", *t0)

    sn = numpy.zeros((nao,nao))
    vj = numpy.zeros_like(dms)
    vk = numpy.zeros_like(dms)

    ngrids = grids.coords.shape[0]
    max_memory = sgx.max_memory - lib.current_memory()[0]
    sblk = sgx.blockdim
    blksize = min(ngrids, max(4, int(min(sblk, max_memory*1e6/8/nao**2))))
    tnuc = 0, 0
    for i0, i1 in lib.prange(0, ngrids, blksize):
        coords = grids.coords[i0:i1]
        ao = mol.eval_gto('GTOval', coords)
        wao = ao * grids.weights[i0:i1,None]
        sn += lib.dot(ao.T, wao)

        fg = lib.einsum('gi,xij->xgj', wao, dms)
        mask = numpy.zeros(i1-i0, dtype=bool)
        for i in range(nset):
            mask |= numpy.any(fg[i]>gthrd, axis=1)
            mask |= numpy.any(fg[i]<-gthrd, axis=1)
        if not numpy.all(mask):
            ao = ao[mask]
            wao = wao[mask]
            fg = fg[:,mask]
            coords = coords[mask]

        if sgx.debug:
            tnuc = tnuc[0] - time.clock(), tnuc[1] - time.time()
            gbn = batch_nuc(mol, coords)
            tnuc = tnuc[0] + time.clock(), tnuc[1] + time.time()
            if with_j:
                jg = numpy.einsum('gij,xij->xg', gbn, dms)
            if with_k:
                gv = lib.einsum('gvt,xgt->xgv', gbn, fg)
            gbn = None
        else:
            tnuc = tnuc[0] - time.clock(), tnuc[1] - time.time()
            jg, gv = batch_jk(mol, coords, dms, fg)
            tnuc = tnuc[0] + time.clock(), tnuc[1] + time.time()

        if with_j:
            xj = lib.einsum('gv,xg->xgv', ao, jg)
            for i in range(nset):
                vj[i] += lib.einsum('gu,gv->uv', wao, xj[i])
        if with_k:
            for i in range(nset):
                vk[i] += lib.einsum('gu,gv->uv', ao, gv[i])
        jg = gv = None

    t2 = logger.timer_debug1(mol, "sgX J/K builder", *t1)
    tdot = t2[0] - t1[0] - tnuc[0] , t2[1] - t1[1] - tnuc[1]
    logger.debug1(sgx, '(CPU, wall) time for integrals (%.2f, %.2f); '
                  'for tensor contraction (%.2f, %.2f)',
                  tnuc[0], tnuc[1], tdot[0], tdot[1])

    ovlp = mol.intor_symmetric('int1e_ovlp')
    proj = scipy.linalg.solve(sn, ovlp)

    if with_j:
        vj = lib.einsum('pi,xpj->xij', proj, vj)
        vj = (vj + vj.transpose(0,2,1))*.5
    if with_k:
        vk = lib.einsum('pi,xpj->xij', proj, vk)
        if hermi == 1:
            vk = (vk + vk.transpose(0,2,1))*.5
    logger.timer(mol, "vj and vk", *t0)
    return vj.reshape(dm_shape), vk.reshape(dm_shape)



#global bvvsh



@profile
def get_jk_favorj(sgx, dm, hermi=1, with_j=True, with_k=True, direct_scf_tol=1e-13):
    t0 = time.clock(), time.time()
    mol = sgx.mol
    grids = sgx.grids
    gthrd = sgx.grids_thrd

    dms = numpy.asarray(dm)
    dm_shape = dms.shape
    nao = dm_shape[-1]
    dms = dms.reshape(-1,nao,nao)
    nset = dms.shape[0]

    if sgx.debug:
        batch_nuc = _gen_batch_nuc(mol)
    else:
        batch_jk = _gen_jk_direct(mol, 's2', with_j, with_k, direct_scf_tol)

    # for basis set to shell
    intor = mol._add_suffix('int3c2e')
    fakemol = gto.fakemol_for_charges(grids.coords)
    atm, bas, env = gto.mole.conc_env(mol._atm, mol._bas, mol._env,
                                      fakemol._atm, fakemol._bas, fakemol._env)
    ao_loc = moleintor.make_loc(bas, intor)
    rao_loc = numpy.zeros((nao),dtype=int)
    for i in range(mol.nbas):
        for j in range(ao_loc[i],ao_loc[i+1]):
            rao_loc[j] = i

    sn = numpy.zeros((nao,nao))
    ngrids = grids.coords.shape[0]
    max_memory = sgx.max_memory - lib.current_memory()[0]
    sblk = sgx.blockdim
    blksize = min(ngrids, max(4, int(min(sblk, max_memory*1e6/8/nao**2))))
    for i0, i1 in lib.prange(0, ngrids, blksize):
        coords = grids.coords[i0:i1]
        ao = mol.eval_gto('GTOval', coords)
        wao = ao * grids.weights[i0:i1,None]
        sn += lib.dot(ao.T, wao)

    ovlp = mol.intor_symmetric('int1e_ovlp')
    proj = scipy.linalg.solve(sn, ovlp)
    proj_dm = lib.einsum('ki,xij->xkj', proj, dms)

    t1 = logger.timer_debug1(mol, "sgX initialziation", *t0)
    vj = numpy.zeros_like(dms)
    vk = numpy.zeros_like(dms)
    tnuc = 0, 0
    for i0, i1 in lib.prange(0, ngrids, blksize):
        coords = grids.coords[i0:i1]
        ao = mol.eval_gto('GTOval', coords)
        wao = ao * grids.weights[i0:i1,None]

        fg = lib.einsum('gi,xij->xgj', wao, proj_dm)
        mask = numpy.zeros(i1-i0, dtype=bool)
        for i in range(nset):
            gmaxfg = numpy.amax(numpy.absolute(fg[i]), axis=1)
            gmaxwao_v = numpy.amax(numpy.absolute(ao), axis=1)
            gmaxtt = gmaxfg * gmaxwao_v
            mask |= numpy.any(gmaxtt>1e-7)
            mask |= numpy.any(gmaxtt<-1e-7)
        if not numpy.all(mask):
            ao = ao[mask]
            wao = wao[mask]
            fg = fg[:,mask]
            coords = coords[mask]

        # screening u by value of grids 
        umaxg = numpy.amax(numpy.absolute(wao), axis=0)
        usi = numpy.argwhere(umaxg > 1e-7).reshape(-1)
        if len(usi) != 0:
            # screening v by ovlp 
            uovl = ovlp[usi, :]
            vmaxu = numpy.amax(numpy.absolute(uovl), axis=0)
            osi = numpy.argwhere(vmaxu > 1e-4).reshape(-1) 
            udms = proj_dm[0][usi, :]
            # screening v by dm and ovlp then triangle matrix bn
            dmaxg = numpy.amax(numpy.absolute(udms), axis=0)
            dsi = numpy.argwhere(dmaxg > 1e-4).reshape(-1) 
            vsi = numpy.intersect1d(dsi, osi)
            if len(vsi) != 0:
                vsh = numpy.unique(rao_loc[vsi])
                mol._bvv = vsh     
      
        # screening u by value of grids 
        umaxg = numpy.amax(numpy.absolute(wao), axis=0)
        usi = numpy.argwhere(umaxg > 1e-7).reshape(-1)
        if len(usi) != 0:
            # screening v by ovlp 
            uovl = ovlp[usi, :]
            vmaxu = numpy.amax(numpy.absolute(uovl), axis=0)
            osi = numpy.argwhere(vmaxu > 1e-4).reshape(-1) 
            if len(osi) != 0:
                vsh = numpy.unique(rao_loc[osi])
                #print(vsh.shape,'eew',vsh)
                mol._bvv = vsh  

        fg = lib.einsum('gi,xij->xgj', wao, proj_dm)
        mask = numpy.zeros(i1-i0, dtype=bool)
        for i in range(nset):
            mask |= numpy.any(fg[i]>gthrd, axis=1)
            mask |= numpy.any(fg[i]<-gthrd, axis=1)
        if not numpy.all(mask):
            ao = ao[mask]
            fg = fg[:,mask]
            coords = coords[mask]

        if with_j:
            rhog = numpy.einsum('xgu,gu->xg', fg, ao)
        else:
            rhog = None

        if sgx.debug:
            tnuc = tnuc[0] - time.clock(), tnuc[1] - time.time()
            gbn = batch_nuc(mol, coords)
            tnuc = tnuc[0] + time.clock(), tnuc[1] + time.time()
            if with_j:
                jpart = numpy.einsum('guv,xg->xuv', gbn, rhog)
            if with_k:
                gv = lib.einsum('gtv,xgt->xgv', gbn, fg)
            gbn = None
        else:
            tnuc = tnuc[0] - time.clock(), tnuc[1] - time.time()
            jpart, gv = batch_jk(mol, coords, rhog, fg)
            tnuc = tnuc[0] + time.clock(), tnuc[1] + time.time()

        if with_j:
            vj += jpart
        if with_k:
            for i in range(nset):
                vk[i] += lib.einsum('gu,gv->uv', ao, gv[i])
        jpart = gv = None

    t2 = logger.timer_debug1(mol, "sgX J/K builder", *t1)
    tdot = t2[0] - t1[0] - tnuc[0] , t2[1] - t1[1] - tnuc[1]
    logger.debug1(sgx, '(CPU, wall) time for integrals (%.2f, %.2f); '
                  'for tensor contraction (%.2f, %.2f)',
                  tnuc[0], tnuc[1], tdot[0], tdot[1])

    for i in range(nset):
        lib.hermi_triu(vj[i], inplace=True)
    if with_k and hermi == 1:
        vk = (vk + vk.transpose(0,2,1))*.5
    logger.timer(mol, "vj and vk", *t0)
    return vj.reshape(dm_shape), vk.reshape(dm_shape)

def _gen_batch_nuc(mol):
    '''Coulomb integrals of the given points and orbital pairs'''
    cintopt = gto.moleintor.make_cintopt(mol._atm, mol._bas, mol._env, 'int3c2e')
    def batch_nuc(mol, grid_coords, out=None):
        fakemol = gto.fakemol_for_charges(grid_coords)
        j3c = aux_e2(mol, fakemol, intor='int3c2e', aosym='s2ij', cintopt=cintopt)
        return lib.unpack_tril(j3c.T, out=out)
    return batch_nuc
@profile
def _gen_jk_direct(mol, aosym, with_j, with_k, direct_scf_tol):
    '''Contraction between sgX Coulomb integrals and density matrices
    J: einsum('guv,xg->xuv', gbn, dms) if dms == rho at grid
       einsum('gij,xij->xg', gbn, dms) if dms are density matrices
    K: einsum('gtv,xgt->xgv', gbn, fg)
    '''
    intor = mol._add_suffix('int3c2e')
    cintopt = gto.moleintor.make_cintopt(mol._atm, mol._bas, mol._env, intor)
    ncomp = 1
    nao = mol.nao

    vhfopt = _vhf.VHFOpt(mol, 'int1e_ovlp', 'SGXnr_ovlp_prescreen',
                         'SGXsetnr_direct_scf')
    vhfopt.direct_scf_tol = direct_scf_tol
    cintor = _vhf._fpointer(intor)
    fdot = _vhf._fpointer('SGXdot_nr'+aosym)
    drv = _vhf.libcvhf.SGXnr_direct_drv

    # for linsgx, from _vhf.VHFOpt
    libcvhf = lib.load_library('libcvhf')
    intor = mol._add_suffix('int1e_ovlp')
    c_atm = numpy.asarray(mol._atm, dtype=numpy.int32, order='C')
    c_bas = numpy.asarray(mol._bas, dtype=numpy.int32, order='C')
    c_env = numpy.asarray(mol._env, dtype=numpy.double, order='C')
    natm = ctypes.c_int(c_atm.shape[0])
    nbas = ctypes.c_int(c_bas.shape[0])

    @profile
    def jk_part(mol, grid_coords, dms, fg):
 
        # transfer bvv to SGXsetnr_direct_scf_blk. from _vhf.VHFOpt
        # need add mol._bvv in scf.mole.py 
        c_bvv = numpy.asarray(mol._bvv, dtype=numpy.int32, order='C')
        nbvv = ctypes.c_int(c_bvv.shape[0])        
        ao_loc = make_loc(c_bas, intor)
        fsetqcond = getattr(libcvhf, 'SGXsetnr_direct_scf_blk')
        fsetqcond(vhfopt._this,
                  getattr(libcvhf, intor), lib.c_null_ptr(),
                  ao_loc.ctypes.data_as(ctypes.c_void_p),
                  c_atm.ctypes.data_as(ctypes.c_void_p), natm,
                  c_bas.ctypes.data_as(ctypes.c_void_p), nbas,
                  c_env.ctypes.data_as(ctypes.c_void_p),
                  c_bvv.ctypes.data_as(ctypes.c_void_p), nbvv
                  )

        fakemol = gto.fakemol_for_charges(grid_coords)
        atm, bas, env = gto.mole.conc_env(mol._atm, mol._bas, mol._env,
                                          fakemol._atm, fakemol._bas, fakemol._env)

        ao_loc = moleintor.make_loc(bas, intor)
        shls_slice = (0, mol.nbas, 0, mol.nbas, mol.nbas, len(bas))
        ngrids = grid_coords.shape[0]

        vj = vk = None
        fjk = []
        dmsptr = []
        vjkptr = []
        if with_j:
            if dms[0].ndim == 1:  # the value of density at each grid
                vj = numpy.zeros((len(dms),ncomp,nao,nao))[:,0]
                for i, dm in enumerate(dms):
                    dmsptr.append(dm.ctypes.data_as(ctypes.c_void_p))
                    vjkptr.append(vj[i].ctypes.data_as(ctypes.c_void_p))
                    fjk.append(_vhf._fpointer('SGXnr'+aosym+'_ijg_g_ij'))
            else:
                vj = numpy.zeros((len(dms),ncomp,ngrids))[:,0]
                for i, dm in enumerate(dms):
                    dmsptr.append(dm.ctypes.data_as(ctypes.c_void_p))
                    vjkptr.append(vj[i].ctypes.data_as(ctypes.c_void_p))
                    fjk.append(_vhf._fpointer('SGXnr'+aosym+'_ijg_ji_g'))
        if with_k:
            vk = numpy.zeros((len(fg),ncomp,ngrids,nao))[:,0]
            for i, dm in enumerate(fg):
                dmsptr.append(dm.ctypes.data_as(ctypes.c_void_p))
                vjkptr.append(vk[i].ctypes.data_as(ctypes.c_void_p))
                fjk.append(_vhf._fpointer('SGXnr'+aosym+'_ijg_gj_gi'))

        n_dm = len(fjk)
        fjk = (ctypes.c_void_p*(n_dm))(*fjk)
        dmsptr = (ctypes.c_void_p*(n_dm))(*dmsptr)
        vjkptr = (ctypes.c_void_p*(n_dm))(*vjkptr)

        drv(cintor, fdot, fjk, dmsptr, vjkptr, n_dm, ncomp,
            (ctypes.c_int*6)(*shls_slice),
            ao_loc.ctypes.data_as(ctypes.c_void_p),
            cintopt, vhfopt._this,
            atm.ctypes.data_as(ctypes.c_void_p), ctypes.c_int(mol.natm),
            bas.ctypes.data_as(ctypes.c_void_p), ctypes.c_int(mol.nbas),
            env.ctypes.data_as(ctypes.c_void_p))
        return vj, vk
    return jk_part

# pre for get_k
# Use default mesh grids and weights
def get_gridss(mol, level=1, gthrd=1e-10):
    Ktime = (time.clock(), time.time())
    grids = dft.gen_grid.Grids(mol)
    grids.level = level
    grids.build()

    ao_v = mol.eval_gto('GTOval', grids.coords)
    ao_v *= grids.weights[:,None]
    wao_v0 = ao_v

    mask = numpy.any(wao_v0>gthrd, axis=1) | numpy.any(wao_v0<-gthrd, axis=1)
    grids.coords = grids.coords[mask]
    grids.weights = grids.weights[mask]
    logger.debug(mol, 'threshold for grids screening %g', gthrd)
    logger.debug(mol, 'number of grids %d', grids.weights.size)
    logger.timer_debug1(mol, "Xg screening", *Ktime)
    return grids

get_jk = get_jk_favorj


if __name__ == '__main__':
    from pyscf import scf
    from pyscf.sgx import sgx
    mol = gto.Mole()
    mol.build(
        verbose = 0,
        atom = [["O" , (0. , 0.     , 0.)],
                [1   , (0. , -0.757 , 0.587)],
                [1   , (0. , 0.757  , 0.587)] ],
        basis = 'ccpvdz',
    )
    dm = scf.RHF(mol).run().make_rdm1()
    vjref, vkref = scf.hf.get_jk(mol, dm)
    print(numpy.einsum('ij,ji->', vjref, dm))
    print(numpy.einsum('ij,ji->', vkref, dm))

    sgxobj = sgx.SGX(mol)
    sgxobj.grids = get_gridss(mol, 0, 1e-10)
    with lib.temporary_env(sgxobj, debug=True):
        vj, vk = get_jk_favork(sgxobj, dm)
    print(numpy.einsum('ij,ji->', vj, dm))
    print(numpy.einsum('ij,ji->', vk, dm))
    print(abs(vjref-vj).max().max())
    print(abs(vkref-vk).max().max())
    with lib.temporary_env(sgxobj, debug=False):
        vj1, vk1 = get_jk_favork(sgxobj, dm)
    print(abs(vj - vj1).max())
    print(abs(vk - vk1).max())

    with lib.temporary_env(sgxobj, debug=True):
        vj, vk = get_jk_favorj(sgxobj, dm)
    print(numpy.einsum('ij,ji->', vj, dm))
    print(numpy.einsum('ij,ji->', vk, dm))
    print(abs(vjref-vj).max().max())
    print(abs(vkref-vk).max().max())

    with lib.temporary_env(sgxobj, debug=False):
        vj1, vk1 = get_jk_favorj(sgxobj, dm)
    print(abs(vj - vj1).max())
    print(abs(vk - vk1).max())
