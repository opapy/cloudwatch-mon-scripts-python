#!/usr/bin/env python

# Copyright 2015 Oliver Siegmar
#
# Based on Perl-Version of CloudWatch Monitoring Scripts for Linux -
# Copyright 2013 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from cloudwatchmon.cloud_watch_client import *

import argparse
import boto
import boto.ec2.autoscale
import boto.ec2.cloudwatch
import datetime
import fnmatch
import os
import random
import re
import sys
import socket
import time

import psutil


CLIENT_NAME = 'CloudWatch-PutInstanceData'
FileCache.CLIENT_NAME = CLIENT_NAME
AWS_LIMIT_METRICS_SIZE = 20

SIZE_UNITS_CFG = {
    'bytes': {'name': 'Bytes', 'div': 1},
    'kilobytes': {'name': 'Kilobytes', 'div': 1024},
    'megabytes': {'name': 'Megabytes', 'div': 1048576},
    'gigabytes': {'name': 'Gigabytes', 'div': 1073741824}
}


class MemData:
    def __init__(self, mem_used_incl_cache_buff):
        self.mem_used_incl_cache_buff = mem_used_incl_cache_buff
        mem_info = self.__gather_mem_info()
        self.mem_total = mem_info['MemTotal']
        self.mem_free = mem_info['MemFree']
        self.mem_cached = mem_info['Cached']
        self.mem_buffers = mem_info['Buffers']
        self.swap_total = mem_info['SwapTotal']
        self.swap_free = mem_info['SwapFree']

    @staticmethod
    def __gather_mem_info():
        mem_info = {}
        pattern = re.compile(r'^(?P<key>\S*):\s*(?P<value>\d*)\s*kB')
        with open('/proc/meminfo') as f:
            for line in f:
                match = pattern.match(line)
                if match:
                    key, value = match.groups(['key', 'value'])
                    mem_info[key] = int(value) * 1024
        return mem_info

    def mem_util(self):
        return 100.0 * self.mem_used() / self.mem_total

    def mem_used(self):
        return self.mem_total - self.mem_avail()

    def mem_avail(self):
        mem_avail = self.mem_free
        if not self.mem_used_incl_cache_buff:
            mem_avail += self.mem_cached + self.mem_buffers

        return mem_avail

    def swap_util(self):
        if self.swap_total == 0:
            return 0

        return 100.0 * self.swap_used() / self.swap_total

    def swap_used(self):
        return self.swap_total - self.swap_free


class LoadAverage:
    def __init__(self):
        load_avg = self.__gather_loadavg_info()
        self.loadavg_1min = load_avg['1min']
        self.loadavg_5min = load_avg['5min']
        self.loadavg_15min = load_avg['15min']
        self.loadavg_percpu_1min = load_avg['percpu_1min']
        self.loadavg_percpu_5min = load_avg['percpu_5min']
        self.loadavg_percpu_15min = load_avg['percpu_15min']

    @staticmethod
    def __gather_loadavg_info():
        loadavg_info = {}

        with open('/proc/loadavg') as loadavg:
            parsed = loadavg.read().split(' ')
            loadavg_info['1min'] = float(parsed[0])
            loadavg_info['5min'] = float(parsed[1])
            loadavg_info['15min'] = float(parsed[2])

        with open('/proc/cpuinfo') as cpuinfo:
            cpu_count = cpuinfo.read().count('processor\t:')
            loadavg_info['percpu_1min'] = loadavg_info['1min'] / cpu_count
            loadavg_info['percpu_5min'] = loadavg_info['5min'] / cpu_count
            loadavg_info['percpu_15min'] = loadavg_info['15min'] / cpu_count

        return loadavg_info


class Disk:
    def __init__(self, mount, file_system, total, used, avail):
        self.mount = mount
        self.file_system = file_system
        self.used = used
        self.avail = avail
        self.util = 100.0 * used / total if total > 0 else 0


class File(object):
    def __init__(self, file_size):
        self.file_size = file_size
        self.file_exists


class Process(object):
    def __init__(self, proc_count):
        self.proc_count = proc_count


class TCPPing(object):
    def __init__(self, check_result):
        self.check_result = check_result


class Metrics:
    def __init__(self, region, instance_id, instance_type, image_id,
                 aggregated, autoscaling_group_name):
        self.names = []
        self.units = []
        self.values = []
        self.dimensions = []
        self.region = region
        self.instance_id = instance_id
        self.instance_type = instance_type
        self.image_id = image_id
        self.aggregated = aggregated
        self.autoscaling_group_name = autoscaling_group_name

    def add_metric(self, name, unit, value, mount=None, file_system=None, f=None, proc=None, tcp_ping=None):
        common_dims = {}
        if mount:
            common_dims['MountPath'] = mount
        if file_system:
            common_dims['Filesystem'] = file_system
        if f:
            common_dims['File'] = f
        if proc:
            common_dims['Process'] = proc
        if tcp_ping:
            common_dims['Target'] = tcp_ping

        dims = []

        if self.aggregated != 'only':
            dims.append({'InstanceId': self.instance_id})

        if self.autoscaling_group_name:
            dims.append({'AutoScalingGroupName': self.autoscaling_group_name})

        if self.aggregated:
            dims.append({'InstanceType': self.instance_type})
            dims.append({'ImageId': self.image_id})
            dims.append({})

        self.__add_metric_dimensions(name, unit, value, common_dims, dims)

    def __add_metric_dimensions(self, name, unit, value, common_dims, dims):
        for dim in dims:
            self.names.append(name)
            self.units.append(unit)
            self.values.append(value)
            self.dimensions.append(dict(common_dims.items() + dim.items()))

    def send(self, verbose):
        boto_debug = 2 if verbose else 0

        # TODO add timeout
        conn = boto.ec2.cloudwatch.connect_to_region(self.region,
                                                     debug=boto_debug)

        if not conn:
            raise IOError('Could not establish connection to CloudWatch')

        size = len(self.names)

        for idx_start in xrange(0, size, AWS_LIMIT_METRICS_SIZE):
            idx_end = idx_start + AWS_LIMIT_METRICS_SIZE
            response = conn.put_metric_data('System/Linux',
                                            self.names[idx_start:idx_end],
                                            self.values[idx_start:idx_end],
                                            datetime.datetime.utcnow(),
                                            self.units[idx_start:idx_end],
                                            self.dimensions[idx_start:idx_end])

            if not response:
                raise ValueError('Could not send data to CloudWatch - '
                                 'use --verbose for more information')

    def __str__(self):
        ret = ''
        for i in range(0, len(self.names)):
            ret += '{0}: {1} {2} ({3})\n'.format(self.names[i],
                                                 self.values[i],
                                                 self.units[i],
                                                 self.dimensions[i])
        return ret


def to_lower(s):
    return s.lower()


def config_parser():
    size_units = ['bytes', 'kilobytes', 'megabytes', 'gigabytes']
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='''
  Collects memory, swap, and disk space utilization on an Amazon EC2 instance
  and sends this data as custom metrics to Amazon CloudWatch.''', epilog='''
Supported UNITS are bytes, kilobytes, megabytes, and gigabytes.

Examples

 To perform a simple test run without posting data to Amazon CloudWatch

  ./put_instance_stats.py --mem-util --verify --verbose
  or
  # If installed via pip install cloudwatchmon
  mon-put-instance-stats.py --mem-util --verify --verbose

 To set a five-minute cron schedule to report memory and disk space utilization
 to CloudWatch

  */5 * * * * ~/cloudwatchmon/put_instance_stats.py --mem-util --disk-space-util --disk-path=/ --from-cron
  or
  # If installed via pip install cloudwatchmon
  * /5 * * * * /usr/local/bin/mon-put-instance-stats.py --mem-util --disk-space-util --disk-path=/ --from-cron

  To report metrics from file
  mon-put-instance-stats.py --from-file filename.csv

For more information on how to use this utility, see project home on GitHub:
https://github.com/osiegmar/cloudwatch-mon-scripts-python
    ''')

    parser.add_argument('--from-file',
                        metavar='FILENAME',
                        action='append',
                        help='Add metrics from file, the metrics data must be in csv format (name,unit,value)')

    memory_group = parser.add_argument_group('memory metrics')
    memory_group.add_argument('--mem-util',
                              action='store_true',
                              help='Reports memory utilization in percentages.')
    memory_group.add_argument('--mem-used',
                              action='store_true',
                              help='Reports memory used in megabytes.')
    memory_group.add_argument('--mem-avail',
                              action='store_true',
                              help='Reports available memory in megabytes.')
    memory_group.add_argument('--swap-util',
                              action='store_true',
                              help='Reports swap utilization in percentages.')
    memory_group.add_argument('--swap-used',
                              action='store_true',
                              help='Reports allocated swap space in megabytes.')
    memory_group.add_argument('--mem-used-incl-cache-buff',
                              action='store_true',
                              help='Count memory that is cached and in buffers as used.')
    memory_group.add_argument('--memory-units',
                              metavar='UNITS',
                              default='megabytes',
                              type=to_lower,
                              choices=size_units,
                              help='Specifies units for memory metrics.')

    loadavg_group = parser.add_argument_group('load average')
    loadavg_group.add_argument('--loadavg',
                               action='store_true',
                               help='Report load averages for 1min, 5min and 15min.')
    loadavg_group.add_argument('--loadavg-percpu',
                               action='store_true',
                               help='Report load averages for 1min, 5min and 15min divided by the number of CPU cores.')


    disk_group = parser.add_argument_group('disk metrics')
    disk_group.add_argument('--disk-path',
                            metavar='PATH',
                            action='append',
                            help='Selects the disk by the path on which to report.')
    disk_group.add_argument('--disk-space-util',
                            action='store_true',
                            help='Reports disk space utilization in percentages.')
    disk_group.add_argument('--disk-space-used',
                            action='store_true',
                            help='Reports allocated disk space in gigabytes.')
    disk_group.add_argument('--disk-space-avail',
                            action='store_true',
                            help='Reports available disk space in gigabytes.')
    disk_group.add_argument('--disk-space-units',
                            metavar='UNITS',
                            default='gigabytes',
                            type=to_lower,
                            choices=size_units,
                            help='Specifies units for disk space metrics.')

    proc_group = parser.add_argument_group('process metrics')
    proc_group.add_argument('--proc-name',
                            metavar='PROC_NAME',
                            action='append',
                            help='Selects the process by the name on which to report.')

    file_group = parser.add_argument_group('file metrics')
    file_group.add_argument('--file-path',
                            metavar='FILE_PATH',
                            action='append',
                            help='Selects the file by the path on which to report.')

    tcp_group = parser.add_argument_group('tcp ping metrics')
    tcp_group.add_argument('--tcp-ping',
                            metavar='HOST:PORT',
                            action='append',
                            help='Selects the tcp ping by the addr on which to report.')

    exclusive_group = parser.add_mutually_exclusive_group()
    exclusive_group.add_argument('--from-cron',
                                 action='store_true',
                                 help='Specifies that this script is running from cron.')
    exclusive_group.add_argument('--verbose',
                                 action='store_true',
                                 help='Displays details of what the script is doing.')

    parser.add_argument('--aggregated',
                        type=to_lower,
                        choices=['additional', 'only'],
                        const='additional',
                        nargs='?',
                        help='Adds aggregated metrics for instance type, AMI id, and overall.')
    parser.add_argument('--auto-scaling',
                        type=to_lower,
                        choices=['additional', 'only'],
                        const='additional',
                        nargs='?',
                        help='Adds aggregated metrics for Auto Scaling group.')
    parser.add_argument('--verify',
                        action='store_true',
                        help='Checks configuration and prepares a remote call.')
    parser.add_argument('--version',
                        action='store_true',
                        help='Displays the version number and exits.')

    return parser


def add_memory_metrics(args, metrics):
    mem = MemData(args.mem_used_incl_cache_buff)

    mem_unit_name = SIZE_UNITS_CFG[args.memory_units]['name']
    mem_unit_div = float(SIZE_UNITS_CFG[args.memory_units]['div'])
    if args.mem_util:
        metrics.add_metric('MemoryUtilization', 'Percent', mem.mem_util())
    if args.mem_used:
        metrics.add_metric('MemoryUsed', mem_unit_name,
                           mem.mem_used() / mem_unit_div)
    if args.mem_avail:
        metrics.add_metric('MemoryAvailable', mem_unit_name,
                           mem.mem_avail() / mem_unit_div)
    if args.swap_util:
        metrics.add_metric('SwapUtilization', 'Percent', mem.swap_util())
    if args.swap_used:
        metrics.add_metric('SwapUsed', mem_unit_name,
                           mem.swap_used() / mem_unit_div)

def add_loadavg_metrics(args, metrics):
    loadavg = LoadAverage()
    if args.loadavg:
        metrics.add_metric('LoadAvg1Min', None, loadavg.loadavg_1min)
        metrics.add_metric('LoadAvg5Min', None, loadavg.loadavg_5min)
        metrics.add_metric('LoadAvg15Min', None, loadavg.loadavg_15min)
    if args.loadavg_percpu:
        metrics.add_metric('LoadAvgPerCPU1Min', None, loadavg.loadavg_percpu_1min)
        metrics.add_metric('LoadAvgPerCPU5Min', None, loadavg.loadavg_percpu_5min)
        metrics.add_metric('LoadAvgPerCPU15Min', None, loadavg.loadavg_percpu_15min)


def get_disk_info(paths):
    df_out = [s.split() for s in
              os.popen('/bin/df -k -P ' +
                       ' '.join(paths)).read().splitlines()]
    disks = []
    for line in df_out[1:]:
        mount = line[5]
        file_system = line[0]
        total = int(line[1]) * 1024
        used = int(line[2]) * 1024
        avail = int(line[3]) * 1024
        disks.append(Disk(mount, file_system, total, used, avail))
    return disks


def add_disk_metrics(args, metrics):
    disk_unit_name = SIZE_UNITS_CFG[args.disk_space_units]['name']
    disk_unit_div = float(SIZE_UNITS_CFG[args.disk_space_units]['div'])
    disks = get_disk_info(args.disk_path)
    for disk in disks:
        if args.disk_space_util:
            metrics.add_metric('DiskSpaceUtilization', 'Percent',
                               disk.util, disk.mount, disk.file_system)
        if args.disk_space_used:
            metrics.add_metric('DiskSpaceUsed', disk_unit_name,
                               disk.used / disk_unit_div,
                               disk.mount, disk.file_system)
        if args.disk_space_avail:
            metrics.add_metric('DiskSpaceAvailable', disk_unit_name,
                               disk.avail / disk_unit_div,
                               disk.mount, disk.file_system)


def get_proc_info(name):
    pids = set()
    for proc in psutil.process_iter():
        if proc.name() == name:
            pids.add(proc.pid)
    return Process(len(pids))


def add_proc_metrics(args, metrics):
    for p in args.proc_name:
        proc = get_proc_info(p)
        metrics.add_metric('ProcessCount', 'Count',  proc.proc_count, proc=p)


def get_tcp_ping_info(host, port, interval=0, retries=3):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    check_result = 0
    for x in range(retries):
        try:
            s.settimeout(1)
            s.connect((host, int(port)))
            s.close()
            check_result = 1
            break
        except socket.error as e:
            time.sleep(interval)

    s.close()
    return TCPPing(check_result)


def add_tcp_ping_metrics(args, metrics):
    for addr in args.tcp_ping:
        host, port = addr.split(':')
        r = get_tcp_ping_info(host, port)
        metrics.add_metric('TCPPing', None, r.check_result, tcp_ping=addr)


def get_file_info(path):
    d = os.path.dirname(path)
    f = os.path.basename(path)

    _files = fnmatch.listdir(d) 
    files = fnmatch.filter(files, f)

    file_size = 0
    file_exists = False

    for i in files:
        fp = os.path.join(d, i)
        file_size += os.path.getsize(fp)
        file_exists = file_exists or os.path.exists(fp)

    file_size = os.path.getsize(path)
    file_exists = os.path.exists(path)

    return File(file_size, file_exists)


def add_file_metrics(args, metrics):
    for f in args.file_path:
        file_info = get_file_info(f)
        metrics.add_metric('FileSize', 'Bytes', file_info.file_size, f=f)
        metrics.add_metric('FileExists', None, file_info.file_exists, f=f)


def add_static_file_metrics(args, metrics):
    with open(args.from_file[0]) as f:
        for line in f.readlines():
            try:
                (label, unit, value) = [x.strip() for x in line.split(',')]
                metrics.add_metric(label, unit, value)
            except ValueError:
                print 'Ignore unparseable metric: "' + line + '"'
                pass


@FileCache
def get_autoscaling_group_name(region, instance_id, verbose):
    boto_debug = 2 if verbose else 0

    # TODO add timeout
    conn = boto.ec2.autoscale.connect_to_region(region, debug=boto_debug)

    if not conn:
        raise IOError('Could not establish connection to CloudWatch')

    autoscaling_instances = conn.get_all_autoscaling_instances([instance_id])

    if not autoscaling_instances:
        raise ValueError('Could not find auto-scaling information')

    return autoscaling_instances[0].group_name


def validate_args(args):
    report_mem_data = args.mem_util or args.mem_used or args.mem_avail or \
        args.swap_util or args.swap_used
    report_disk_data = args.disk_path is not None
    report_loadavg_data = args.loadavg or args.loadavg_percpu
    report_file_data = args.file_path is not None
    report_tcp_ping_data = args.tcp_ping is not None
    report_proc_data = args.proc_name is not None

    if report_disk_data:
        if not args.disk_space_util and not args.disk_space_used and \
                not args.disk_space_avail:
            raise ValueError('Disk path is provided but metrics to report '
                             'disk space are not specified.')

        for path in args.disk_path:
            if not os.path.isdir(path):
                raise ValueError('Disk file path ' + path +
                                 ' does not exist or cannot be accessed.')
    elif args.disk_space_util or args.disk_space_used or \
            args.disk_space_avail:
        raise ValueError('Metrics to report disk space are provided but '
                         'disk path is not specified.')

    if not report_mem_data and not report_disk_data and not args.from_file and \
            not report_loadavg_data and not report_file_data and \
            not report_tcp_ping_data and not report_proc_data:
        raise ValueError('No metrics specified for collection and '
                         'submission to CloudWatch.')

    return report_disk_data, report_mem_data, report_loadavg_data, report_file_data, \
            report_tcp_ping_data, report_proc_data


def main():
    parser = config_parser()

    # exit with help, because no args specified
    if len(sys.argv) == 1:
        parser.print_help()
        return 1

    args = parser.parse_args()

    if args.version:
        print CLIENT_NAME + ' version ' + VERSION
        return 0

    try:
        report_disk_data, report_mem_data, report_loadavg_data, \
                report_file_data, report_tcp_ping_data, report_proc_data  = validate_args(args)

        # avoid a storm of calls at the beginning of a minute
        if args.from_cron:
            time.sleep(random.randint(0, 19))

        if args.verbose:
            print 'Working in verbose mode'
            print 'Boto-Version: ' + boto.__version__

        metadata = get_metadata()

        if args.verbose:
            print 'Instance metadata: ' + str(metadata)

        region = metadata['placement']['availability-zone'][:-1]
        instance_id = metadata['instance-id']
        autoscaling_group_name = None
        if args.auto_scaling:
            autoscaling_group_name = get_autoscaling_group_name(region,
                                                                instance_id,
                                                                args.verbose)

            if args.verbose:
                print 'Autoscaling group: ' + autoscaling_group_name

        metrics = Metrics(region,
                          instance_id,
                          metadata['instance-type'],
                          metadata['ami-id'],
                          args.aggregated,
                          autoscaling_group_name)

        if args.from_file:
            add_static_file_metrics(args, metrics)

        if report_mem_data:
            add_memory_metrics(args, metrics)

        if report_loadavg_data:
            add_loadavg_metrics(args, metrics)

        if report_disk_data:
            add_disk_metrics(args, metrics)

        if report_file_data:
            add_file_metrics(args, metrics)

        if report_tcp_ping_data:
            add_tcp_ping_metrics(args, metrics)

        if report_proc_data:
            add_proc_metrics(args, metrics)

        if args.verbose:
            print 'Request:\n' + str(metrics)

        if args.verify:
            if not args.from_cron:
                print 'Verification completed successfully. ' \
                      'No actual metrics sent to CloudWatch.'
        else:
            metrics.send(args.verbose)
            if not args.from_cron:
                print 'Successfully reported metrics to CloudWatch.'
    except Exception as e:
        log_error(str(e), args.from_cron)
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
