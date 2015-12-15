#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
\descr: The benchmark, winch optionally generates or preprocesses datasets using specified executable,
	optionally executes specified apps with the specified params on the specified datasets,
	and optionally evaluates results of the execution using specified executable(s).

	All executions are traced and logged also as resources consumption: CPU (user, kernel, etc.) and memory (RSS RAM).
	Traces are saved even in case of internal / external interruptions and crashes.

	= Overlapping Hierarchical Clustering Benchmark =
	Implemented:
	- synthetic datasets are generated using extended LFR Framework (origin: https://sites.google.com/site/santofortunato/inthepress2,
		which is "Benchmarks for testing community detection algorithms on directed and weighted graphs with overlapping communities"
		by Andrea Lancichinetti 1 and Santo Fortunato)
	- executes HiReCS (www.lumais.com/hirecs), Louvain (original https://sites.google.com/site/findcommunities/ and igraph implementations),
		Oslom2 (http://www.oslom.org/software.htm) and Ganxis/SLPA (https://sites.google.com/site/communitydetectionslpa/) clustering algorithms
		on the generated synthetic networks
	- evaluates results using NMI for overlapping communities, extended versions of:
		* gecmi (https://bitbucket.org/dsign/gecmi/wiki/Home, "Comparing network covers using mutual information" by Alcides Viamontes Esquivel, Martin Rosvall)
		* onmi (https://github.com/aaronmcdaid/Overlapping-NMI, "Normalized Mutual Information to evaluate overlapping community finding algorithms"
		  by  Aaron F. McDaid, Derek Greene, Neil Hurley)
	- resources consumption is evaluated using exectime profiler (https://bitbucket.org/lumais/exectime/)

\author: (c) Artem Lutov <artem@exascale.info>
\organizations: eXascale Infolab <http://exascale.info/>, Lumais <http://www.lumais.com/>, ScienceWise <http://sciencewise.info/>
\date: 2015-04
"""

from __future__ import print_function  # Required for stderr output, must be the first import
import sys
import time
import subprocess
from multiprocessing import cpu_count
import os
import shutil
import signal  # Intercept kill signals
from math import sqrt
import glob
from itertools import chain

import benchapps  # Benchmarking apps (clustering algs)
from benchcore import *
from benchutils import *

## Add 3dparty modules
##sys.path.insert(0, '3dparty')  # Note: this operation might lead to ambiguity on paths resolving
#thirdparty = __import__('3dparty.tohig')
#tohig = thirdparty.tohig.tohig  # ~ from 3dparty.tohig import tohig

#from functools import wraps
from benchapps import pyexec
from benchapps import evalAlgorithm
from benchcore import _extexectime
from benchcore import _extclnodes
from benchcore import _execpool
from benchapps import _algsdir
from benchapps import _resdir
from benchapps import _sepinst
from benchapps import _seppars


# Note: '/' is required in the end of the dir to evaluate whether it is already exist and distinguish it from the file
_syntdir = 'syntnets/'  # Default directory for the synthetic datasets
_netsdir = 'networks/'  # Networks directory inside syntnets
_syntinum = 5  # Default number of instances of each synthetic network
_extnetfile = '.nsa'  # Extension of the network files to be executed by the algorithms; Network specified by tab/space separated arcs
#_algseeds = 9  # TODO: Implement
_prefExec = 'exec'  # Execution prefix for the apps functions in benchapps


def parseParams(args):
	"""Parse user-specified parameters

	return
		gensynt  - generate synthetic networks:
			0 - do not generate
			1 - generate only if this network is not exist
			2 - force geration (overwrite all)
		netins  - number of network instances for each network type to be generated, >= 1
		shufnum  - number of shuffles of each network instance to be produced, >= 0
		syntdir  - base directory for synthetic datasets
		convnets  - convert existing networks into the .hig format
			0 - do not convert
			0b001  - convert:
				0b01 - convert only if this network is not exist
				0b11 - force conversion (overwrite all)
			0b100 - resolve duplicated links on conversion
		datas  - list of datasets to be run with asym flag (asymmetric / symmetric links weights):
			[(<asym>, <path>, <gendir>), ...] , where path is either dir or file
		timeout  - execution timeout in sec per each algorithm
		algorithms  - algorithms to be executed (just names as in the code)
	"""
	assert isinstance(args, (tuple, list)) and args, 'Input arguments must be specified'
	gensynt = 0
	netins = _syntinum  # Number of network instances to generate, >= 1
	shufnum = 0  # Number of shuffles for each network instance to be produced, >=0
	syntdir = _syntdir  # Base directory for synthetic datasets
	convnets = 0
	runalgs = False
	evalres = 0  # 1 - NMIs, 2 - Q, 3 - all measures
	datas = []  # list of pairs: (<asym>, <path>), where path is either dir or file
	#asym = None  # Asymmetric dataset, per dataset
	timeout = 36 * 60*60  # 36 hours
	timemul = 1  # Time multiplier, sec by default
	algorithms = None

	for arg in args:
		# Validate input format
		if arg[0] != '-':
			raise ValueError('Unexpected argument: ' + arg)

		if arg[1] == 'g':
			gensynt = 1  # Generate if not exists
			alen = len(arg)
			if alen == 2:
				continue
			pos = arg.find('=', 2)
			if arg[2] not in 'f=' or alen == pos + 1:
				raise ValueError('Unexpected argument: ' + arg)
			if arg[2] == 'f':
				gensynt = 2  # Forced generation (overwrite)
			if pos != -1:
				# Parse number of instances, shuffles and outpdir:  [<instances>][.<shuffles>][=<outpdir>]
				val = arg[pos+1:].split('=', 1)
				if val[0]:
					# Parse number of instances
					nums = val[0].split('.', 1)
					# Now [instances][shuffles][outpdir]
					if nums[0]:
						netins = int(nums[0])
					else:
						netins = 0  # Zero if omitted in case of shuffles are specified
					# Parse shuffles
					if len(nums) > 1:
						shufnum = int(nums[1])
					if netins < 0 or shufnum < 0:
						raise ValueError('Value is out of range:  netins: {netins} >= 1, shufnum: {shufnum} >= 0'
							.format(netins=netins, shufnum=shufnum))
				# Parse outpdir
				if len(val) > 1:
					if not val[1]:  # arg ended with '=' symbol
						raise ValueError('Unexpected argument: ' + arg)
					syntdir = val[1]
					syntdir = syntdir.strip('"\'')
					if not syntdir.endswith('/'):
						syntdir += '/'
		elif arg[1] == 'a':
			if not (arg[0:3] == '-a=' and len(arg) >= 4):
				raise ValueError('Unexpected argument: ' + arg)
			algorithms = arg[3:].strip('"\'').split()
		elif arg[1] == 'c':
			convnets = 1
			for i in range(2,4):
				if len(arg) > i and (arg[i] not in 'fr'):
					raise ValueError('Unexpected argument: ' + arg)
			arg = arg[2:]
			if 'f' in arg:
				convnets |= 0b10
			if 'r' in arg:
				convnets |= 0b100
		elif arg[1] == 'r':
			if arg != '-r':
				raise ValueError('Unexpected argument: ' + arg)
			runalgs = True
		elif arg[1] == 'e':
			for i in range(2,4):
				if len(arg) > i and (arg[i] not in 'nm'):
					raise ValueError('Unexpected argument: ' + arg)
			if len(arg) in (2, 4):
				evalres = 3  # all
			# Here len(arg) >= 3
			elif arg[2] == 'n':
				evalres = 1  # NMIs
			else:
				evalres = 2  # Q (modularity)
		elif arg[1] == 'd' or arg[1] == 'f':
			pos = arg.find('=', 2)
			if pos == -1 or arg[2] not in 'gas=' or len(arg) == pos + 1:
				raise ValueError('Unexpected argument: ' + arg)
			# Extend weighted / unweighted dataset, default is unweighted
			val = arg[2]
			gen = False  # Generate dir for this network or not
			if val == 'g':
				gen = True
				val = arg[3]
			asym = None  # Asym: None - not specified (symmetric is assumed), False - symmetric, True - asymmetric
			if val == 'a':
				asym = True
			elif val == 's':
				asym = False
			datas.append((asym, arg[pos+1:].strip('"\''), gen))
		elif arg[1] == 't':
			pos = arg.find('=', 2)
			if pos == -1 or arg[2] not in 'smh=' or len(arg) == pos + 1:
				raise ValueError('Unexpected argument: ' + arg)
			pos += 1
			if arg[2] == 'm':
				timemul = 60  # Minutes
			elif arg[2] == 'h':
				timemul = 3600  # Hours
			timeout = float(arg[pos:]) * timemul
		else:
			raise ValueError('Unexpected argument: ' + arg)

	return gensynt, netins, shufnum, syntdir, convnets, runalgs, evalres, datas, timeout, algorithms


def prepareInput(datas):
	"""Generating directories structure, linking there the original network, and shuffles
	for the input datasets according to the specidied parameters. The former dir is backuped.

	datas  - pathes with flags to be processed in the format: [(<asym>, <path>, <gendir>), ...]

	return
		datadirs  - target dirs of networks to be processed: [(<asym>, <path>), ...]
		datafiles  - target networks to be processed: [(<asym>, <path>), ...]
	"""
	datadirs = []
	datafiles = []

	if not datas:
		return datadirs, datafiles
	assert len(datas[0]) == 3, 'datas must be a container of items of 3 subitems'

	def prepareDir(dirname, netfile, bcksuffix=None):
		"""Move specified dir to the backup if not empty. Make the dir if not exists.
		Link the origal network inside the dir.

		dirname  - dir to be moved
		netfile  - network file to be linked into the <dirname> dir
		bcksuffix  - backup suffix for the group of directories
		"""
		if os.path.exists(dirname) and not dirempty(dirname):
			backupPath(dirname, False, bcksuffix)
		if not os.path.exists(dirname):
			os.mkdir(dirname)
		# Make hard link to the network.
		# Hard link is used to have initial former copy in the archive even when the origin is changed
		os.link(netfile, '/'.join((dirname, os.path.split(netfile)[1])))

	for asym, wpath, gen in datas:
		# Resolve wildcards
		for path in glob.iglob(wpath):
			if gen:
				bcksuffix = SyncValue()  # Use inified syffix for the backup of various network instances
			if os.path.isdir(path):
				# Use the same path separator on all OSs
				if not path.endswith('/'):
					path += '/'
				# Generate dirs if required
				if gen:
					# Traverse over the networks instances and create corresponding dirs
					for net in glob.iglob('*'.join((path, _extnetfile))):
						# Backup existent dir
						dirname = os.path.splitext(net)[0]
						prepareDir(dirname, net, bcksuffix)
						# Update target dirs
						datadirs.append((asym, dirname + '/'))
				else:
					datadirs.append((asym, path))
			else:
				# Generate dirs if required
				if gen:
					dirname = os.path.splitext(path)[0]
					prepareDir(dirname, path, bcksuffix)
					datafiles.append((asym, '/'.join((dirname, os.path.split(path)[1]))))
				else:
					datafiles.append((asym, path))
	return datadirs, datafiles


def generateNets(genbin, basedir, overwrite=False, count=_syntinum, gentimeout=2*60*60):  # 2 hour
	"""Generate synthetic networks with ground-truth communities and save generation params.
	Previously existed paths with the same name are backuped.

	genbin  - the binary used to generate the data
	basedir  - base directory where data will be generated
	overwrite  - whether to overwrite existing networks or use them
	count  - number of insances of each network to be generated, >= 1
	"""
	paramsdir = 'params/'  # Contains networks generation parameters per each network type
	seedsdir = 'seeds/'  # Contains network generation seeds per each network instance
	netsdir = _netsdir  # Contains subdirs, each contains all instances of each network and all shuffles of each instance
	# Note: shuffles unlike ordinary networks have double extension: shuffling nimber and standard extension

	# Store all instances of each network with generation parameters in the dedicated directory
	assert count >= 1, 'Number of the network instances to be generated must be positive'
	assert (basedir[-1] == '/' and paramsdir[-1] == '/' and seedsdir[-1] == '/' and netsdir[-1] == '/'
		), "Directory name must have valid terminator"

	paramsdirfull = basedir + paramsdir
	seedsdirfull = basedir + seedsdir
	netsdirfull = basedir + netsdir
	# Backup params dirs on rewriting
	if overwrite:
		bcksuffix = SyncValue()
		for dirname in (paramsdirfull, seedsdirfull, netsdirfull):
			if os.path.exists(dirname) and not dirempty(dirname):
				backupPath(dirname, False, bcksuffix)

	# Create dirs if required
	if not os.path.exists(basedir):
		os.mkdir(basedir)
	for dirname in (paramsdirfull, seedsdirfull, netsdirfull):
		if not os.path.exists(dirname):
			os.mkdir(dirname)

	# Initial options for the networks generation
	N0 = 1000;  # Satrting number of nodes
	evalmaxk = lambda genopts: int(round(sqrt(genopts['N'])))
	evalmuw = lambda genopts: genopts['mut'] * 2/3
	evalminc = lambda genopts: 5 + int(genopts['N'] / N0)
	evalmaxc = lambda genopts: int(genopts['N'] / 3)
	evalon = lambda genopts: int(genopts['N'] * genopts['mut']**2)
	# Template of the generating options files
	genopts = {'mut': 0.275, 'beta': 1.35, 't1': 1.65, 't2': 1.3, 'om': 2, 'cnl': 1}

	# Generate options for the networks generation using chosen variations of params
	varNmul = (1, 2, 5, 10, 25, 50)  # *N0 - sizes of the generating networks
	vark = (5, 10)  #, 20)  # Average density of network links
	global _execpool

	if not _execpool:
		_execpool = ExecPool(max(cpu_count() - 1, 1))
	netgenTimeout = 15 * 60  # 15 min
	#shuftimeout = 1 * 60  # 1 min per each shuffling
	bmname =  os.path.split(genbin)[1]  # Benchmark name
	bmbin = './' + bmname  # Benchmark binary
	timeseed = basedir + 'time_seed.dat'

	# Check whether time seed exists and create it if required
	if not os.path.exists(timeseed):  # Note: overwrite is not relevant here
		proc = subprocess.Popen((bmbin), bufsize=-1, cwd=basedir)
		proc.wait()
		assert os.path.exists(timeseed), timeseed + ' must be created'
	for nm in varNmul:
		N = nm * N0
		for k in vark:
			name = 'K'.join((str(nm), str(k)))
			ext = '.ngp'  # Network generation parameters
			# Generate network parameters files if not exist
			fnamex = name.join((paramsdirfull, ext))
			if overwrite or not os.path.exists(fnamex):
				print('Generating {} parameters file...'.format(fnamex))
				with open(fnamex, 'w') as fout:
					genopts.update({'N': N, 'k': k})
					genopts.update({'maxk': evalmaxk(genopts), 'muw': evalmuw(genopts), 'minc': evalminc(genopts)
						, 'maxc': evalmaxc(genopts), 'on': evalon(genopts), 'name': name})
					for opt in genopts.items():
						fout.write(''.join(('-', opt[0], ' ', str(opt[1]), '\n')))
			else:
				assert os.path.isfile(fnamex), '{} should be a file'.format(fnamex)
			# Generate networks with ground truth corresponding to the parameters
			if os.path.isfile(fnamex):  # TODO: target
				netpath = name.join((netsdir, '/'))  # syntnets/networks/<netname>/  netname.*
				netparams = name.join((paramsdir, ext))  # syntnets/params/<netname>.<ext>
				# Generate required number of network instances
				if _execpool:
					netpathfull = basedir + netpath
					if not os.path.exists(netpathfull):
						os.mkdir(netpathfull)
					task = Task(name)  # Required to use task.name as basedir identifier
					startdelay = 0.1  # Required to start execution of the LFR benchmark before copying the time_seed for the following process
					netfile = netpath + name
					if count and overwrite or not os.path.exists(netfile.join((basedir, _extnetfile))):
						args = ('../exectime', '-n=' + name, ''.join(('-o=', bmname, _extexectime))  # Output .rcp in the current dir, basedir
							, bmbin, '-f', netparams, '-name', netfile)
						#Job(name, workdir, args, timeout=0, ontimeout=False, onstart=None, ondone=None, tstart=None)
						_execpool.execute(Job(name=name, task=task, workdir=basedir, args=args, timeout=netgenTimeout, ontimeout=True
							, onstart=lambda job: shutil.copy2(timeseed, job.name.join((seedsdirfull, '.ngs')))  # Network generation seed
							#, ondone=shuffle if shufnum > 0 else None
							, startdelay=startdelay))
					#else:
					#	# Create missing shufflings
					#	shuffle(Job(name=name, task=task))
					for i in range(1, count):
						namext = ''.join((name, _sepinst, str(i)))
						netfile = netpath + namext
						if overwrite or not os.path.exists(netfile.join((basedir, _extnetfile))):
							args = ('../exectime', '-n=' + namext, ''.join(('-o=', bmname, _extexectime))
								, bmbin, '-f', netparams, '-name', netfile)
							#Job(name, workdir, args, timeout=0, ontimeout=False, onstart=None, ondone=None, tstart=None)
							_execpool.execute(Job(name=namext, task=task, workdir=basedir, args=args, timeout=netgenTimeout, ontimeout=True
								, onstart=lambda job: shutil.copy2(timeseed, job.name.join((seedsdirfull, '.ngs')))  # Network generation seed
								#, ondone=shuffle if shufnum > 0 else None
								, startdelay=startdelay))
						#else:
						#	# Create missing shufflings
						#	shuffle(Job(name=namext, task=task))
			else:
				print('ERROR: network parameters file "{}" is not exist'.format(fnamex), file=sys.stderr)
	print('Parameter files generation is completed')
	if _execpool:
		_execpool.join(max(gentimeout, count * (netgenTimeout  #+ (shufnum * shuftimeout)
			)))  # 2 hours
		_execpool = None
	print('Synthetic networks files generation is completed')


def shuffleNets(datadirs, datafiles, shufnum, overwrite=False, shuftimeout=30*60):  # 30 min
	"""Shuffle specified networks

	datadirs  - directories with target networks to be processed
	datafiles  - target networks to be processed
	shufnum  - number of shufflings for of each instance on the generated network, > 0
	overwrite  - whether to renew existent shuffles (delete former and generate new).
		ATTENTION: Anyway redundant shuffles are deleted.
	shuftimeout  - global shuffling timeout
	"""
	# Note: backup is performe on paths extraction, see prepareInput()
	assert shufnum >= 1, 'Number of the network shuffles to be generated must be positive'
	global _execpool

	if not _execpool:
		_execpool = ExecPool(max(cpu_count() - 1, 1))

	timeout = 3 * 60  # 3 min per each shuffling

	def shuffle(job):
		"""Shufle network specified by the job"""
		if shufnum < 1:
			return
		args = (pyexec, '-c',
# Shuffling procedure
"""import os
import subprocess

basenet = '{jobname}' + '{_extnetfile}'
#print('basenet: ' + basenet, file=sys.stderr)
for i in range(1, {shufnum} + 1):
	# sort -R pgp_udir.net -o pgp_udir_rand3.net
	netfile = ''.join(('{jobname}', '.', str(i), '{_extnetfile}'))
	if {overwrite} or not os.path.exists(netfile):
		subprocess.call(('sort', '-R', basenet, '-o', netfile))
# Remove existent redundant shuffles if any
#i = {shufnum} + 1
#while i < 100:  # Max number of shuffles
#	netfile = ''.join(('{jobname}', '.', str(i), '{_extnetfile}'))
#	if not os.path.exists(netfile):
#		break
#	else:
#		os.remove(netfile)
""".format(jobname=job.name, _extnetfile=_extnetfile, shufnum=shufnum, overwrite=overwrite))
		_execpool.execute(Job(name=job.name + '_shf', task=job.task, workdir=job.workdir  # + job.task.name
			, args=args, timeout=timeout * shufnum))

	def shuffleNet(netfile):
		"""Shuffle specified network

		return
			shufnum - number of shufflings to be done
		"""
		# Remove existent shuffles if required
		path, name = os.path.split(netfile)
		name = os.path.splitext(name)[0]
		ext2 = os.path.splitext(name)[1]  # Second part of the name (second extension)
		# Omit shuffling of the shuffles
		if ext2:
			# Remove redundant shuffles
			if int(ext2[1:]) > shufnum:
				os.remove(netfile)
			return 0
		task = Task(name)  # Required to use task.name as basedir identifier
		shuffle(Job(name=name, task=task, workdir=path + '/'))
		return shufnum

	count = 0
	for asym, ddir in datadirs:
		for dfile in glob.iglob('*'.join((ddir, _extnetfile))):
			count += shuffleNet(dfile)
	for asym, dfile in datafiles:
		count += shuffleNet(dfile)

	if _execpool:
		_execpool.join(max(shuftimeout, count * shufnum * timeout))  # 30 min
		_execpool = None
	print('Synthetic networks files generation is completed')


def convertNet(inpnet, asym, overwrite=False, resdub=False, timeout=3*60):  # 3 min
	"""Convert input networks to another formats

	datadir  - directory of the networks to be converted
	asym  - network has asymmetric links weights (in/outbound weights can be different)
	overwrite  - whether to overwrite existing networks or use them
	resdub  - resolve duplicated links
	timeout  - network conversion timeout
	"""
	try:
		## Convert to .hig format
		## Network in the tab separated weighted arcs format
		#args = ['-f=ns' + ('a' if asym else 'e'), '-o' + ('f' if overwrite else 's')]
		#if resdub:
		#	args.append('-r')
		#tohig(inpnet, args)

		args = [pyexec, '3dparty/tohig.py', inpnet, '-f=ns' + ('a' if asym else 'e'), '-o' + ('f' if overwrite else 's')]
		if resdub:
			args.append('-r')
		_execpool.execute(Job(name=os.path.splitext(os.path.split(inpnet)[1])[0], args=args, timeout=timeout))

	except StandardError as err:
		print('ERROR on "{}" conversion into .hig, the network is skipped: {}'.format(inpnet, err), file=sys.stderr)
	#netnoext = os.path.splitext(net)[0]  # Remove the extension

	## Confert to Louvain binaty input format
	#try:
	#	# ./convert [-r] -i graph.txt -o graph.bin -w graph.weights
	#	# r  - renumber nodes
	#	# ATTENTION: original Louvain implementation processes incorrectly weighted networks with uniform weights (=1) if supplied as unweighted
	#	subprocess.call((_algsdir + 'convert', '-i', net, '-o', netnoext + '.lig'
	#		, '-w', netnoext + '.liw'))
	#except StandardError as err:
	#	print('ERROR on "{}" conversion into .lig, the network is skipped: {}'.format(net), err, file=sys.stderr)

	## Make shuffled copies of the input networks for the Louvain_igraph
	##if not os.path.exists(netnoext) or overwrite:
	#print('Shuffling {} into {} {} times...'.format(net, netnoext, _netshuffles))
	#if not os.path.exists(netnoext):
	#	os.makedirs(netnoext)
	#netname = os.path.split(netnoext)[1]
	#assert netname, 'netname should be defined'
	#for i in range(_netshuffles):
	#	outpfile = ''.join((netnoext, '/', netname, '_', str(i), _extnetfile))
	#	if overwrite or not sys.path.exists(outpfile):
	#		# sort -R pgp_udir.net -o pgp_udir_rand3.net
	#		subprocess.call(('sort', '-R', net, '-o', outpfile))
	##else:
	##	print('The shuffling is skipped: {} is already exist'.format(netnoext))


def convertNets(datadir, asym, overwrite=False, resdub=False, convtimeout=30*60):  # 30 min
	"""Convert input networks to another formats

	datadir  - directory of the networks to be converted
	asym  - network links weights are asymmetric (in/outbound weights can be different)
	overwrite  - whether to overwrite existing networks or use them
	resdub  - resolve duplicated links
	"""
	print('Converting networks from {} into the required formats (.hig, .lig, etc.)...'
		.format(datadir))

	global _execpool

	if not _execpool:
		_execpool = ExecPool(max(cpu_count() - 1, 1))

	convTimeMax = 3 * 60  # 3 min
	netsnum = 0  # Number of converted networks
	# Convert network files to .hig format and .lig (Louvain Input Format)
	for net in glob.iglob('*'.join((datadir, _extnetfile))):
		# Skip shuffles
		if not os.path.splitext(os.path.splitext(net)[0])[1]:
			convertNet(net, asym, overwrite, resdub, convTimeMax)
			netsnum += 1
	## Convert network files to .hig format and .lig (Louvain Input Format)
	#for net in glob.iglob('*'.join((datadir, _extnetfile))):
	#	# Check existence of the corresponding dir with shuffled files
	#	netdir = os.path.splitext(net)[0]
	#	if os.path.exists(netdir):
	#		for net in glob.iglob('/*'.join((netdir, _extnetfile))):
	#			convertNet(net, asym, overwrite, resdub, convTimeMax)
	#			netsnum += 1
	#	else:
	#		# Convert the original
	#		convertNet(net, asym, overwrite, resdub, convTimeMax)
	#		netsnum += 1
	## Traverse direct subfolders if target networks are not directly in the folder
	#if not netsnum:
	#	for netdir in glob.iglob(datadir + '*'):
	#		# Convert networks in subdirs
	#		if os.path.isdir(netdir):
	#			for net in glob.iglob('/*'.join((netdir, _extnetfile))):
	#				convertNet(net, asym, overwrite, resdub, convTimeMax)
	#				netsnum += 1

	if _execpool:
		_execpool.join(max(convtimeout, netsnum * convTimeMax))  # 2 hours
		_execpool = None
	print('Networks conversion is completed, converted {} networks'.format(netsnum))


def runApps(appsmodule, algorithms, datadirs, datafiles, exectime, timeout):
	"""Run specified applications (clustering algorithms) on the specified datasets

	appsmodule  - module with algorithms definitions to be run; sys.modules[__name__]
	algorithms  - list of the algorithms to be executed
	datadirs  - directories with target networks to be processed
	datafiles  - target networks to be processed
	exectime  - elapsed time since the benchmarking started
	timeout  - timeout per each algorithm execution
	"""
	assert appsmodule and (datadirs or datafiles) and exectime >= 0 and timeout >= 0, 'Invalid input arguments'

	global _execpool

	assert not _execpool, '_execpool should be clear on algs execution'
	starttime = time.time()  # Procedure start time
	if not _execpool:
		_execpool = ExecPool(max(min(4, cpu_count() - 1), 1))

	# Run all algs if not specified the concrete algorithms to be run
	#udatas = ['../snap/com-dblp.ungraph.txt', '../snap/com-amazon.ungraph.txt', '../snap/com-youtube.ungraph.txt']
	if not algorithms:
		#algs = (execLouvain, execHirecs, execOslom2, execGanxis, execHirecsNounwrap)
		#algs = (execHirecsNounwrap,)  # (execLouvain, execHirecs, execOslom2, execGanxis, execHirecsNounwrap)
		# , execHirecsOtl, execHirecsAhOtl, execHirecsNounwrap)  # (execLouvain, execHirecs, execOslom2, execGanxis, execHirecsNounwrap)
		algs = [getattr(appsmodule, func) for func in dir(appsmodule) if func.startswith(_prefExec)]
	else:
		algs = [getattr(appsmodule, _prefExec + alg.capitalize(), unknownApp(_prefExec + alg.capitalize())) for alg in algorithms]
	algs = tuple(algs)

	def execute(net, asym, jobsnum):
		"""Execute algorithms on the specified network counting number of ran jobs

		net  - network to be processed
		asym  - network links weights are asymmetric (in/outbound weights can be different)
		jobsnum  - accumulated number of scheduled jobs

		return
			jobsnum  - updated accumulated number of scheduled jobs
		"""
		for alg in algs:
			try:
				jobsnum += alg(_execpool, net, asym, timeout)
			except StandardError as err:
				errexectime = time.time() - exectime
				print('The {} is interrupted by the exception: {} on {:.4f} sec ({} h {} m {:.4f} s)'
					.format(alg.__name__, err, errexectime, *secondsToHms(errexectime)))
		return jobsnum

	jobsnum = 1  # Number of networks jobs to be processed (can be a few per each algorithm per each network)
	netcount = 0  # Number of networks to be processed
	for asym, ddir in datadirs:
		for net in glob.iglob('*'.join((ddir, _extnetfile))):
			tnum = execute(net, asym, jobsnum)
			jobsnum += tnum
			netcount += tnum != 0
	for asym, net in datafiles:
		tnum = execute(net, asym, jobsnum)
		jobsnum += tnum
		netcount += tnum != 0

	if _execpool:
		timelim = min(timeout * jobsnum, 5 * 24*60*60)  # Global timeout, up to N days
		print('Waiting for the algorithms execution on {} jobs from {} networks'
			' with {} sec ({} h {} m {:.4f} s) timeout'.format(jobsnum, netcount, timelim, *secondsToHms(timelim)))
		_execpool.join(timelim)
		_execpool = None
	starttime -= time.time() - starttime
	print('The apps execution is successfully completed, it took {:.4f} sec ({} h {} m {:.4f} s)'
		.format(starttime, *secondsToHms(starttime)))


def evalResults(evalres, appsmodule, algorithms, datadirs, datafiles, exectime, timeout):
	"""Run specified applications (clustering algorithms) on the specified datasets

	evalres  - evaluation flags: 0 - Skip evaluations, 1 - NMIs, 2 - Q (modularity), 3 - all measures
	appsmodule  - module with algorithms definitions to be run; sys.modules[__name__]
	algorithms  - list of the algorithms to be executed
	datadirs  - directories with target networks to be processed
	datafiles  - target networks to be processed
	exectime  - elapsed time since the benchmarking started
	timeout  - timeout per each evaluation run
	"""
	assert (evalres and appsmodule and (datadirs or datafiles) and exectime >= 0
		and timeout >= 0), 'Invalid input arguments'

	global _execpool

	assert not _execpool, '_execpool should be clear on algs evaluation'
	starttime = time.time()  # Procedure start time
	if not _execpool:
		_execpool = ExecPool(max(cpu_count() - 1, 1))

	# Measures is a dict with the Array values: <evalcallback_prefix>, <grounttruthnet_extension>, <measure_name>
	measures = {1: ['nmi', _extclnodes, 'NMI'], 2: ['mod', '.hig', 'Q']}
	for im in measures:
		# Evaluate only required measures
		if evalres & im != im:
			continue

		if not algorithms:
			#evalalgs = (evalLouvain, evalHirecs, evalOslom2, evalGanxis
			#				, evalHirecsNS, evalOslom2NS, evalGanxisNS)
			#evalalgs = (evalHirecs, evalHirecsOtl, evalHirecsAhOtl
			#				, evalHirecsNS, evalHirecsOtlNS, evalHirecsAhOtlNS)
			# Fetch available algorithms
			ianame = len(_prefExec)  # Index of the algorithm name start
			evalalgs = [funcname[ianame:].lower() for funcname in dir(appsmodule) if func.startswith(_prefExec)]
		else:
			evalalgs = [alg.lower() for alg in algorithms]
		evalalgs = tuple(evalalgs)

		def evaluate(measure, basefile, asym, jobsnum):
			"""Evaluate algorithms on the specified network

			measure  - target measure to be evaluated: {nmi, mod}
			basefile  - ground truth result, or initial network file or another measure-related file
			asym  - network links weights are asymmetric (in/outbound weights can be different)
			jobsnum  - accumulated number of scheduled jobs

			return
				jobsnum  - updated accumulated number of scheduled jobs
			"""
			for elgname in evalalgs:
				try:
					evalAlgorithm(_execpool, elgname, basefile, measure, timeout)
					# Evaluate also nmi-s for nmi
					if measure == 'nmi':
						evalAlgorithm(_execpool, elgname, basefile, 'nmi-s', timeout)
				except StandardError as err:
					print('The {} is interrupted by the exception: {}'.format(elgname, err))
				else:
					jobsnum += 1
			return jobsnum

		print('Starting {} evaluation...'.format(measures[im][2]))
		jobsnum = 0
		measure = measures[im][0]
		fileext = measures[im][1]
		for asym, ddir in datadirs:
			# Read ground truth
			for basefile in glob.iglob('*'.join((ddir, fileext))):
				evaluate(measure, basefile, asym, jobsnum)
		for asym, basefile in datafiles:
			# Use files with required extension
			basefile = os.path.splitext(basefile)[0] + fileext
			evaluate(basefile, asym, jobsnum)
		print('{} evaluation is completed'.format(measures[im][2]))
	if _execpool:
		timelim = min(timeout * jobsnum, 5 * 24*60*60)  # Global timeout, up to N days
		_execpool.join(max(timelim, exectime * 2))  # Twice the time of algorithms execution
		_execpool = None
	starttime -= time.time() - starttime
	print('Results evaluation is successfully completed, it took {:.4f} sec ({} h {} m {:.4f} s)'
		.format(starttime, *secondsToHms(starttime)))


def benchmark(*args):
	"""Execute the benchmark

	Run the algorithms on the specified datasets respecting the parameters.
	"""
	exectime = time.time()  # Benchmarking start time

	gensynt, netins, shufnum, syntdir, convnets, runalgs, evalres, datas, timeout, algorithms = parseParams(args)
	print('The benchmark is started, parsed params:\n\tgensynt: {}\n\tsyntdir: {}\n\tconvnets: 0b{:b}'
		'\n\trunalgs: {}\n\tevalres: {}\n\tdatas: {}\n\ttimeout: {}\n\talgorithms: {}'
		.format(gensynt, syntdir, convnets, runalgs, evalres
			, ', '.join(['{}{}{}'.format('' if not asym else 'asym: ', path, ' (gendir)' if gen else '')
				for asym, path, gen in datas])
			, timeout, algorithms))
	# Make syntdir and link there lfr benchmark bin if required
	bmname = 'lfrbench_udwov'  # Benchmark name
	benchpath = syntdir + bmname  # Benchmark path
	if not os.path.exists(syntdir):
		os.makedirs(syntdir)
		# Symlink is used to work even when target file is on another file system
		os.symlink(os.path.relpath(_syntdir + bmname, syntdir), benchpath)

	# Extract dirs and files from datas, generate dirs structure and shuffles if required
	datadirs, datafiles = prepareInput(datas)
	datas = None
	#print('Datadirs: ', datadirs)

	if gensynt and netins >= 1:
		# gensynt:  0 - do not generate, 1 - only if not exists, 2 - forced generation
		generateNets(benchpath, syntdir, gensynt == 2, netins)

	# Update datasets with sythetic generated
	# Note: should be done only after the genertion, because new directories can be created
	if gensynt or (not datadirs and not datafiles):
		datadirs.append((False, _netsdir.join((syntdir, '*/'))))  # asym, ddir

	if shufnum:
		shuffleNets(datadirs, datafiles, shufnum, gensynt == 2)

	# convnets: 0 - do not convert, 0b01 - only if not exists, 0b11 - forced conversion, 0b100 - resolve duplicated links
	if convnets:
		for asym, ddir in datadirs:
			convertNets(ddir, asym, convnets&0b11 == 0b11, convnets&0b100)
		for asym, dfile in datafiles:
			convertNet(dfile, asym, convnets&0b11 == 0b11, convnets&0b100)

	# Run the algorithms and measure their resource consumption
	if runalgs:
		runApps(benchapps, algorithms, datadirs, datafiles, exectime, timeout)

	# Evaluate results
	if evalres:
		evalResults(evalres, benchapps, algorithms, datadirs, datafiles, exectime, timeout)

	exectime = time.time() - exectime
	print('The benchmark is completed, it took {:.4f} sec ({} h {} m {:.4f} s)'
		.format(exectime, *secondsToHms(exectime)))


def terminationHandler(signal, frame):
	"""Signal termination handler"""
	#if signal == signal.SIGABRT:
	#	os.killpg(os.getpgrp(), signal)
	#	os.kill(os.getpid(), signal)

	global _execpool

	if _execpool:
		del _execpool
		_execpool = None
	sys.exit(0)


if __name__ == '__main__':
	if len(sys.argv) > 1:
		# Set handlers of external signals
		signal.signal(signal.SIGTERM, terminationHandler)
		signal.signal(signal.SIGHUP, terminationHandler)
		signal.signal(signal.SIGINT, terminationHandler)
		signal.signal(signal.SIGQUIT, terminationHandler)
		signal.signal(signal.SIGABRT, terminationHandler)
		benchmark(*sys.argv[1:])
	else:
		print('\n'.join(('Usage: {0} [-g[f][=[<number>][.<shuffles_number>][=<outpdir>]] [-c[f][r]] [-a="app1 app2 ..."]'
			' [-r] [-e[n][m]] [-d[g]{{a,s}}=<datasets_dir>] [-f[g]{{a,s}}=<dataset>] [-t[{{s,m,h}}]=<timeout>]',
			'Parameters:',
			'  -g[f][=[<number>][.<shuffles_number>][=<outpdir>]]  - generate <number> ({synetsnum} by default) >= 0'
			' synthetic datasets in the <outpdir> ("{syntdir}" by default), shuffling each <shuffles_number>'
			' (0 by default) >= 0 times. If <number> is omitted or set to 0 then ONLY shuffling of the specified datasets'
			' should be performed including the <outpdir>/{netsdir}/*.',
			'    Xf  - force the generation even when the data already exists (existent datasets are moved to backup)',
			'  NOTE:',
			'    - shuffled datasets have the following naming format:'
			' <base_name>[{sepinst}<instance_index>][(seppars)<param1>...][.<shuffle_index>].<net_extension>',
			'    - use "-g0" to execute existing synthetic networks not changing them',
			'  -c[X]  - convert existing networks into the .hig, .lig, etc. formats',
			'    Xf  - force the conversion even when the data is already exist',
			'    Xr  - resolve (remove) duplicated links on conversion. Note: this option is recommended to be used',
			'  NOTE: files with {extnetfile} are looked for in the specified dirs to be converted',
			'  -a="app1 app2 ..."  - apps (clustering algorithms) to run/benchmark among the implemented.'
			' Available: scp louvain_ig randcommuns hirecs oslom2 ganxis.'
			' Impacts -{{r, e}} options. Optional, all apps are executed by default.',
			'  NOTE: output results are stored in the "algorithms/<algname>outp/" directory',
			'  -r  - run the benchmarking apps on the prepared data',
			#'    Xf  - force execution even when the results already exists (existent datasets are moved to backup)',
			'  -e[[X]  - evaluate quality of the results. Default: apply all measurements',
			#'    Xf  - force execution even when the results already exists (existent datasets are moved to backup)',
			'    Xn  - evaluate results accuracy using NMI measures for overlapping communities',
			'    Xm  - evaluate results quality by modularity',
			# TODO: customize extension of the network files (implement filters)
			'  -d[X]=<datasets_dir>  - directory of the datasets.',
			'  -f[X]=<dataset>  - dataset (network, graph) file name.',
			'    Xg  - generate directory with the network file name without extension for each input network (*{extnetfile}).'
			' It can be used to avoid flooding of the dirctory with networks with shuffles of each network, previously'
			' existed shuffles are backuped',
			'    Xa  - the dataset is specified by asymmetric links (in/outbound weights of the link might differ), arcs',
			'    Xs  - the dataset is specified by symmetric links, edges. Default option',
			'    NOTE:',
			'	 - datasets file names must not contain "." (besides the extension),'
			' because it is used as indicator of the shuffled datasets',
			'    - paths can contain wildcards: *, ?, +'
			'    - multiple directories and files can be specified via multiple -d/f options (one per the item)',
			'    - datasets should have the following format: <node_src> <node_dest> [<weight>]',
			'    - {{a,s}} is considered only if the network file has no corresponding metadata (formats like SNAP, ncol, nsa, ...)',
			'    - ambiguity of links weight resolution in case of duplicates (or edges specified in both directions)'
			' is up to the clustering algorithm',
			'  -t[X]=<float_number>  - specifies timeout for each benchmarking application per single evaluation on each network'
			' in sec, min or hours. Default: 0 sec  - no timeout',
			'    Xs  - time in seconds. Default option',
			'    Xm  - time in minutes',
			'    Xh  - time in hours',
			)).format(sys.argv[0], syntdir=_syntdir, synetsnum=_syntinum, netsdir=_netsdir, sepinst=_sepinst
				, seppars=_seppars, extnetfile=_extnetfile))
