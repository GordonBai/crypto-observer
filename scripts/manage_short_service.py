"""Manage the independent, public-data short-term observer LaunchAgent."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
LABEL='com.gordonbai.crypto-short-observer'
TARGET=f'gui/{os.getuid()}/{LABEL}'
PLIST=Path.home()/'Library'/'LaunchAgents'/(LABEL+'.plist')


def run(args):
    return subprocess.run(['launchctl',*args],capture_output=True,text=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['install','status','stop'])
    parser.add_argument('--interval',type=int,default=300)
    args=parser.parse_args()
    if not 60<=args.interval<=300:
        parser.error('short-term interval must be between 60 and 300 seconds')
    if args.action=='status':
        result=run(['print',TARGET])
        if result.returncode:
            print(result.stderr.strip());return result.returncode
        fields={k:v for k,v in re.findall(r'^\s*(state|pid|runs|last exit code|run interval) = (.+)$',result.stdout,re.M)}
        print(json.dumps({'label':LABEL,'plist':str(PLIST),**fields},indent=2))
        return 0
    if args.action=='stop':
        result=run(['bootout',TARGET])
        print(result.stdout or result.stderr or 'job stopped; plist preserved')
        return result.returncode
    if sys.platform!='darwin':
        parser.error('use short-cycle with your process manager on non-macOS hosts')
    if PLIST.exists() and plistlib.loads(PLIST.read_bytes()).get('WorkingDirectory')!=str(ROOT):
        raise RuntimeError('service label belongs to another workspace')
    result=run(['print',TARGET])
    if result.returncode==0:
        print('service already registered; current job left intact')
        return 0
    (ROOT/'data').mkdir(exist_ok=True)
    config={'Label':LABEL,
      'ProgramArguments':[sys.executable,'-B','-m','crypto_observer',
         '--db',str(ROOT/'data/short_term_v2.sqlite3'),'short-cycle',
         '--output-dir',str(ROOT/'reports/short_term'),'--market-limit','36'],
      'WorkingDirectory':str(ROOT),'StartInterval':args.interval,'RunAtLoad':True,
      'ProcessType':'Background','LowPriorityIO':True,
      'EnvironmentVariables':{'PYTHONUNBUFFERED':'1','PATH':'/opt/homebrew/bin:/usr/bin:/bin'},
      'StandardOutPath':str(ROOT/'data/short_service.stdout.log'),
      'StandardErrorPath':str(ROOT/'data/short_service.stderr.log')}
    PLIST.parent.mkdir(parents=True,exist_ok=True)
    PLIST.write_bytes(plistlib.dumps(config));PLIST.chmod(0o600)
    result=run(['bootstrap',f'gui/{os.getuid()}',str(PLIST)])
    if result.returncode:
        print(result.stderr,file=sys.stderr)
    else:
        print(json.dumps({'label':LABEL,'interval_seconds':args.interval,'database':str(ROOT/'data/short_term_v2.sqlite3')}))
    return result.returncode

if __name__=='__main__':
    raise SystemExit(main())
