#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
\descr: Overlapping Hierarhical Clusterig Benchmark

# Generates synthetic datasets for reusing
# https://sites.google.com/site/santofortunato/inthepress2
# "Benchmarks for testing community detection algorithms on directed and weighted graphs with overlapping communities" by Andrea Lancichinetti 1 and Santo Fortunato

Runs hierarchical clustering algorithms on the synthetic networks and real-word datasets

(c) 
\author: Artem Lutov <artem@exascale.info>
\organizations: eXascale lab <http://exascale.info/>, ScienceWise <http://sciencewise.info/>, Lumais <http://www.lumais.com/>
\date: 2015-04
"""

import sys
import time
import subprocess
from subprocess import PIPE
from functools import wraps


def parseParams(args):
	"""Parse user-specified parameters
	
	return
		udatas  - list of unweighted datasets to be run
		wdatas  - list of weighted datasets to be run
		timeout  - execution timeout in sec per each algorithm
	"""
	assert isinstance(args, (tuple, list)) and args, 'Input arguments must be specified'
	udatas = []
	wdatas = []
	timeout = 0
	sparam = False  # Additional string parameter
	weighted = False
	timemul = 1  # Time multiplier, sec by default
	for arg in args:
		# Validate input format
		if (arg[0] != '-') != bool(sparam) or (len(arg) < 2 if arg[0] == '-' else arg in '..'):
			raise ValueError(''.join(('Unexpected argument', ', file/dir name is expected: ' if sparam else ': ', arg)))
		
		if arg[0] == '-':
			if arg[1] == 'd' or arg[1] == 'f':
				weighted = False
				sparam = 'd'  # Dataset
				if len(arg) >= 3:
					if arg[2] not in 'uw' or len(arg) > 3:
						raise ValueError('Unexpected argument: ' + arg)
					weighted = arg[2] == 'w'
			elif arg[1] == 't':
				sparam = 't'  # Time
				if len(arg) >= 3:
					if arg[2] not in 'smh' or len(arg) > 3:
						raise ValueError('Unexpected argument: ' + arg)
					if arg[2] == 'm':
						multiplier = 60  # Minutes
					elif arg[2] == 'h':
						multiplier = 3600  # Hours
			else:
				raise ValueError('Unexpected argument: ' + arg)
		else:
			assert sparam in 'dt', "sparam should be either dataset file/dir or time"
			if sparam == 'd':
				(wdatas if weighted else udatas).append(arg)
			elif sparam == 't':
				timeout = int(arg) * timemul
			else:
				raise RuntimeError('Unexpected value of sparam: ' + sparam)
			sparam = False
	
	return udatas, wdatas, timeout


def secondsToHms(seconds):
	"""Convert seconds to hours, mins, secs
	
	seconds  - seconds to be converted
	
	return hours, mins, secs
	"""
	hours = int(seconds / 3600)
	mins = int((seconds - hours * 3600) / 60)
	secs = seconds - hours * 3600 - mins * 60
	return hours, mins, secs
	

def controlTime(proc, algname, exectime, timeout):
	"""Conterol the time of the executing process
	
	Evaluates execution time and kills the process after the specified timeout
	
	proc  - active executing process
	algname  - name of the executing algorithm
	exectime  - start time of the execution
	timeout  - execution timeout, 0 means infinity
	"""
	while proc.poll() is None:
		time.sleep(1)
		if timeout and time.clock() - exectime > timeout:
			exectime = time.clock() - exectime
			proc.terminate()
			# Wait 10 sec for the successful process termitaion before killing it
			i = 0
			while proc.poll() is None and i < 10:
				i += 1
				time.sleep(1)
			if proc.poll() is None:
				proc.kill()
			print('{} is terminated by the timeout ({} sec): {} secs ({} h {} m {} s)'
				.format(algname, timeout, exectime, *secondsToHms(exectime)))


def execAlgorithm(algname, workdir, args, timeout, trace=True):
	"""Execute specified algorithm
	
	algname  - algorithm name (id)
	workdir  - working directory
	args  - execution arguments including the executable itself
	timeout  - execution timeout
	trace  - whether to trace execution steps
	"""
	assert algname and workdir and args, ""
	
	# Execution block
	if trace:
		print(algname + ' is starting...')
	try:
		exectime = time.clock()
		proc = subprocess.Popen(args, cwd=workdir)  # bufsize=-1 - use system default IO buffer size
	except StandardError as err:  # Should not occur: subprocess.CalledProcessError
		print('ERROR on {} execution occurred: {}'.format(algname, err))
	else:
		controlTime(proc, algname, exectime, timeout)
	if trace:
		print(algname + ' is finished.\n\n\n')


def execLouvain(udatas, wdatas, timeout):
	algname = 'Louvain'
	workdir = 'LouvainUpd'

	# Preparation block
	#...

	args = ['../exectime', 'ls']
	execAlgorithm(algname, workdir, args, timeout)

	# Postprocessing block
	#...


def execHirecs(udatas, wdatas, timeout):
	algname = 'HiReCS'
	workdir = '.'
	args = ['./hirecs']
	execAlgorithm(algname, workdir, args, timeout)


def execOslom2(udatas, wdatas, timeout):
	pass


def execGanxis(udatas, wdatas, timeout):
	pass


def benchmark(*args):
	""" Execute the benchmark:
	Run the algorithms on the specified datasets respecting the parameters
	"""
	exectime = time.clock()
	udatas, wdatas, timeout = parseParams(args)
	print("Parsed params:\n\tudatas: {}, \n\twdatas: {}\n\ttimeout: {}"
		.format(', '.join(udatas), ', '.join(wdatas), timeout))
	
	algors = (execLouvain, execHirecs, execOslom2, execGanxis)
	try:
		#algtime = time.clock()
		for alg in algors:
			alg(udatas, wdatas, timeout)
	except StandardError as err:
		print('The benchmark is interrupted by the exception: {}'.format(err))
	else:
		exectime = time.clock() - exectime
		print('The benchmark execution is successfully comleted on {} sec ({} h {} m {} s)'
			.format(exectime, *secondsToHms(exectime)))


if __name__ == '__main__':
	if len(sys.argv) > 1:
		benchmark(*sys.argv[1:])
	else:
		print('\n'.join(('Usage: {0} [-d{{u,w}} <datasets_dir>] [-f{{u,w}} <dataset>] [-t[{{s,m,h}}] <timeout>]',
			'  -d[X] <datasets_dir>  - directory of the datasets',
			'  -f[X] <dataset>  - dataset file name',
			'    Xu  - the dataset is unweighted. Default option',
			'    Xw  - the dataset is weighted',
			'    Notes:',
			'    - multiple directories and files can be specified',
			'    - datasets should have the following format: <node_src> <node_dest> [<weight>]',
			'  -t[X] <number>  - specifies timeout per an algorithm in sec, min or hours. Default: 0 sec',
			'    Xs  - time in seconds. Default option',
			'    Xm  - time in minutes',
			'    Xh  - time in hours',
			))
			.format(sys.argv[0]))