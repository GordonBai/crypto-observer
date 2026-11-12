import argparse
import fcntl
import json
import time
from pathlib import Path
from .core import Client, Store, collect, report
from .security import scan, cases
from .onchain import probe
from .paper import open_positions, evaluate, summary
from .evaluation import report as evaluation_report
from .holder_index import index as index_holders, report as holder_report
from .runtime import cycle
from .audit import report as audit_report
from .review import check_quotes
from .short_windows import report as window_report


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description="Base public-data observer")
    parser.add_argument("--db", default=None)
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("collect", "loop"):
        p = subs.add_parser(name)
        p.add_argument("--pages", type=int, choices=range(1, 11), default=2)
        p.add_argument("--refresh-limit", type=positive, default=100)
        if name == "loop":
            p.add_argument("--cycles", type=positive, default=4)
            p.add_argument("--interval", type=positive, default=300)
    p = subs.add_parser("short-cycle")
    p.add_argument("--output-dir",default="reports/short_term")
    p.add_argument("--market-limit",type=positive,default=36)
    p = subs.add_parser("market-collect")
    p.add_argument("--limit",type=positive,default=36)
    for name in ("market-report","short-report"):
        p = subs.add_parser(name)
        p.add_argument("--output")
    p = subs.add_parser("review-quotes")
    p.add_argument("--limit",type=positive,default=20)
    p = subs.add_parser("windows-report")
    p.add_argument("--output")
    p = subs.add_parser("audit")
    p.add_argument("--output")
    p = subs.add_parser("cycle")
    p.add_argument("--output-dir",default="reports")
    p.add_argument("--pages",type=int,choices=range(1,11),default=3)
    p.add_argument("--refresh-limit",type=positive,default=300)
    p.add_argument("--holder-limit",type=positive,default=1)
    p = subs.add_parser("holder-index")
    p.add_argument("--limit",type=positive,default=1)
    p.add_argument("--chunks",type=positive,default=4)
    p.add_argument("--token")
    p = subs.add_parser("holder-report")
    p.add_argument("--output")
    p = subs.add_parser("evaluate-report")
    p.add_argument("--output")
    p = subs.add_parser("paper-open")
    p.add_argument("--limit",type=positive,default=2)
    subs.add_parser("paper-evaluate")
    p = subs.add_parser("paper-report")
    p.add_argument("--output")
    p = subs.add_parser("probe")
    p.add_argument("--limit",type=positive,default=3)
    p = subs.add_parser("scan")
    p.add_argument("--limit", type=positive, default=20)
    p = subs.add_parser("cases")
    p.add_argument("--limit", type=positive, default=20)
    p.add_argument("--output")
    p = subs.add_parser("report")
    p.add_argument("--output")
    args = parser.parse_args(argv)
    short_commands = {"short-cycle","market-collect","market-report","short-report"}
    path = Path(args.db or ("data/short_term_v2.sqlite3" if args.command in short_commands else "data/observer.sqlite3")).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = None
    if args.command != "report":
        lock = open(str(path) + ".lock", "a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            parser.exit(2, "another collector is using this database\n")
    store = Store(path)
    try:
        if args.command in short_commands:
            from .market_data import collect_market, report as market_report
            from .short_experiment import report as short_report
            from .short_runtime import cycle as short_cycle
            if args.command in ("market-report","short-report"):
                result = market_report(store) if args.command=="market-report" else short_report(store)
                if args.output:
                    output=Path(args.output)
                    output.parent.mkdir(parents=True,exist_ok=True)
                    output.write_text(result)
                    print(output.resolve())
                else:
                    print(result)
                return 0
            result = (collect_market(store,Client(),args.limit) if args.command=="market-collect"
                      else short_cycle(store,Client(),args.output_dir,args.market_limit))
            print(json.dumps(result),flush=True)
            return 1 if result.get("status")=="failed" else 0
        if args.command in ("report", "cases", "paper-report", "evaluate-report", "holder-report", "audit", "windows-report"):
            result = window_report(store) if args.command == "windows-report" else report(store) if args.command == "report" else summary(store) if args.command=="paper-report" else evaluation_report(store) if args.command=="evaluate-report" else holder_report(store) if args.command=="holder-report" else audit_report(store) if args.command=="audit" else cases(store,args.limit)
            if args.output:
                output = Path(args.output)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(result)
                print(output.resolve())
            else:
                print(result)
            return 0
        client = Client()
        if args.command=="review-quotes":
            print(json.dumps(check_quotes(store,client,args.limit)),flush=True)
            return 0
        if args.command=="cycle":
            result=cycle(store,client,args.output_dir,args.pages,args.refresh_limit,args.holder_limit)
            print(json.dumps(result),flush=True)
            return 1 if result["status"]=="failed" else 0
        if args.command=="holder-index":
            print(json.dumps(index_holders(store,client,args.limit,args.chunks,token=args.token)),flush=True)
            return 0
        if args.command in ("paper-open","paper-evaluate"):
            result = open_positions(store,client,args.limit) if args.command=="paper-open" else evaluate(store,client)
            print(json.dumps(result),flush=True)
            return 0
        if args.command in ("scan","probe"):
            result = scan(store,client,args.limit) if args.command=="scan" else probe(store,client,args.limit)
            print(json.dumps(result),flush=True)
            return 1 if result["notes"] else 0
        exit_code = 0
        for i in range(args.cycles if args.command == "loop" else 1):
            result = collect(store, client, args.pages, args.refresh_limit)
            print(json.dumps(result), flush=True)
            exit_code = 1 if result["notes"] else exit_code
            if args.command == "loop" and i+1 < args.cycles:
                time.sleep(args.interval)
        return exit_code
    except KeyboardInterrupt:
        return 130
    finally:
        store.close()
        if lock:
            lock.close()
