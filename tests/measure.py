#!/usr/bin/env python3
"""One fixed matched comparison over the independently frozen synthetic corpora."""
from pathlib import Path
import argparse, hashlib, json, math, os, resource, shutil, statistics, struct, subprocess, sys, time, traceback
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures as frozen


def pin(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for data in iter(lambda:stream.read(1024*1024),b''): h.update(data)
    return {'bytes':path.stat().st_size,'sha256':h.hexdigest()}


def equal_files(a,b):
    with a.open('rb') as left,b.open('rb') as right:
        while True:
            x,y=left.read(1024*1024),right.read(1024*1024)
            if x!=y: raise AssertionError('byte mismatch: '+str(a)+' / '+str(b))
            if not x: return


def observation(path):
    return {'meminfo':{line.split(':')[0]:line.split(':')[1].strip() for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith(('MemAvailable:','MemTotal:'))},'loadavg':Path('/proc/loadavg').read_text().strip(),'disk':dict(zip(('total','used','free'),shutil.disk_usage(path))),'affinity':sorted(os.sched_getaffinity(0))}


def validate_metrics(mode, metric, query, lengths, record_count):
    def duration(value):
        assert isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value>=0
    assert metric['record_count']==record_count and metric['query_count']==len(query)
    assert len(metric['passes'])==3
    payload_bytes=sum(lengths)
    if mode in ('grouped','per-record','monolithic'):
        assert metric['schema_version']==1 and metric['passes_count']==3
        for key in ('wall_seconds','cpu_seconds'):duration(metric['open'][key])
        for number, row in enumerate(metric['passes'],1):
            assert row['pass']==number and len(row['queries'])==len(query)
            assert row['payload_bytes']==payload_bytes
            for key in ('wall_seconds','cpu_seconds','read_wall_seconds_sum','read_cpu_seconds_sum'):duration(row[key])
            for observed,ordinal,length in zip(row['queries'],query,lengths):
                assert observed['ordinal']==ordinal and observed['payload_bytes']==length
                duration(observed['wall_seconds']);duration(observed['cpu_seconds'])
                assert isinstance(observed['bytes_read'],int) and observed['bytes_read']>=0
                assert isinstance(observed['bytes_decoded'],int) and observed['bytes_decoded']>=0
    else:
        assert metric['status']=='PASS' and metric['mode']==mode
        for key in ('open_wall_ns','open_cpu_ns'):duration(metric[key])
        for number,row in enumerate(metric['passes'],1):
            assert row['pass']==number and len(row['query_latencies_ns'])==len(query)
            assert row['payload_bytes']==payload_bytes
            assert row['canonical_output_bytes']==payload_bytes+8*len(query)
            assert row['query_latency']['count']==len(query)
            for value in row['query_latencies_ns']:duration(value)
            for key in ('wall_ns','cpu_ns','query_time_sum_ns'):duration(row[key])
        assert metric['payloads_bytes']==3*(payload_bytes+8*len(query))


class Calls:
    def __init__(self,out): self.out=out;self.rows=[]
    def run(self,label,command):
        n=len(self.rows);stem=self.out/('call-%03d'%n)
        process=None;status=None;usage=None;failure=None
        before=observation(self.out);start=time.time();wall=time.perf_counter()
        try:
            with stem.with_suffix('.stdout').open('xb') as stdout,stem.with_suffix('.stderr').open('xb') as stderr:
                env=os.environ.copy();env['TMPDIR']=str(self.out/'.tmp');env['PYTHONDONTWRITEBYTECODE']='1'
                process=subprocess.Popen(list(map(str,command)),cwd=self.out,env=env,stdout=stdout,stderr=stderr)
                _,status,usage=os.wait4(process.pid,0)
                process.returncode=os.waitstatus_to_exitcode(status)
        except BaseException as error:
            failure=type(error).__name__+': '+str(error)
            if process is not None and status is None:
                process.terminate()
                _,status,usage=os.wait4(process.pid,0)
                process.returncode=os.waitstatus_to_exitcode(status)
        row={'label':label,'command':list(map(str,command)),'pid':None if process is None else process.pid,'started_unix':start,'ended_unix':time.time(),'wall_seconds':time.perf_counter()-wall,'exit_code':None if status is None else os.waitstatus_to_exitcode(status),'cpu_seconds':None if usage is None else usage.ru_utime+usage.ru_stime,'peak_rss_bytes':None if usage is None else usage.ru_maxrss*1024,'failure':failure,'resources_before':before,'stdout':pin(stem.with_suffix('.stdout')),'stderr':pin(stem.with_suffix('.stderr'))}
        self.rows.append(row);(self.out/'CALLS.json').write_text(json.dumps(self.rows,indent=2)+'\n')
        if failure or row['exit_code']!=0: raise RuntimeError(label+' failed; see retained native receipt')
        return row


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--candidate',type=Path,required=True)
    parser.add_argument('--bench',type=Path,required=True)
    parser.add_argument('--baseline',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True);(args.output/'.tmp').mkdir()
    controls=[Path(__file__),Path(frozen.__file__),Path(__file__).resolve().parents[1] / 'docs' / 'FORMAT.md',args.candidate,args.bench,args.baseline,Path('/usr/bin/zstd')]
    before={str(p.resolve()):pin(p) for p in controls}
    calls=Calls(args.output);results=[]
    result={'status':'FAILED','source_before':before,'timing_scope':'Fresh native processes; three same-order repeated-query passes within each open. OS cache is not cleared. ZIP/raw/cache use Python baseline runtime; zstd framed modes use the same Ada binding.',
            'clock_and_flush_scope':'Ada CPU clocks measure the current task. Per-query clocks exclude payload output and metric collection. Ada pass clocks include Stream_IO.Write calls but exclude final Close/flush; Python payload writes are unbuffered. Compare per-query read latency across modes, and native process totals for full cost including closure.',
            'counter_scope':'Ada counters are bytes requested through application reads and decoded frame bytes, not kernel I/O. ZIP compressed-payload counts exclude headers/metadata/buffering. Raw counts are returned header+payload bytes; cache decodes once at open.',
            'memory_scope':'Native peak_rss_bytes is the child process-lifetime wait4 rusage high-water and may include pre-exec memory. It is not a post-exec worker VmHWM measurement or a simultaneous process-tree total. Cache logical payload/offset bytes are reported separately.'}
    try:
        for name in frozen.COST_NAMES:
            out=args.output/name;out.mkdir()
            items=list(frozen.records(name))
            canonical=out/'input.records'
            with canonical.open('xb') as stream:
                for chunk in frozen.canonical_chunks(items):stream.write(chunk)
            groups=[len(group) for group in frozen.expected_groups(items)]
            query=frozen.queries(len(items),groups)
            lengths=[len(items[q]) for q in query]
            query_file=out/'queries.txt';query_file.write_text(''.join(str(q)+'\n' for q in query))
            expected=out/'expected.payloads'
            with expected.open('xb') as stream:
                for _ in range(3):
                    for q in query:
                        stream.write(struct.pack('<Q',len(items[q])));stream.write(items[q])
            case={'name':name,'record_count':len(items),'input':pin(canonical),'group_counts':groups,'query_count':len(query),'query_file':pin(query_file),'expected_payloads':pin(expected),'modes':{}}
            del items
            for mode in ('grouped','per-record','monolithic'):
                archive=out/(mode+'.zst')
                build=calls.run(name+' '+mode+' pack',[args.candidate,'pack',mode,canonical,archive])
                calls.run(name+' '+mode+' verify',[args.candidate,'verify',archive])
                unpacked=out/(mode+'.unpacked')
                calls.run(name+' '+mode+' unpack',[args.candidate,'unpack',archive,unpacked])
                equal_files(canonical,unpacked)
                info_call=calls.run(name+' '+mode+' info',[args.candidate,'info',archive])
                info=json.loads((args.output/('call-%03d.stdout'%(len(calls.rows)-1))).read_text())
                metric=out/(mode+'.bench.json')
                bench=calls.run(name+' '+mode+' bench',[args.bench,archive,query_file,metric])
                equal_files(expected,Path(str(metric)+'.payloads'))
                measured=json.loads(metric.read_text());validate_metrics(mode,measured,query,lengths,frozen.COST_RECORD_COUNT)
                case['modes'][mode]={'archive':pin(archive),'pack_native':build,'bench_native':bench,'info':info,'metrics':measured,'exact_all_records_and_queries':True}
            offsets=out/'raw.offsets'
            raw_build=calls.run(name+' raw index',[sys.executable,'-B',args.baseline,'raw-build',canonical,offsets])
            zipped=out/'records.zip'
            zip_build=calls.run(name+' zip pack',[sys.executable,'-B',args.baseline,'zip-build',canonical,zipped])
            import zipfile
            zip_unpacked=out/'zip.unpacked'
            with zipfile.ZipFile(zipped) as archive,zip_unpacked.open('xb') as stream:
                expected_names=['%08d'%i for i in range(frozen.COST_RECORD_COUNT)]
                assert archive.namelist()==expected_names
                for key in expected_names:
                    payload=archive.read(key);stream.write(struct.pack('<Q',len(payload)));stream.write(payload)
            equal_files(canonical,zip_unpacked)
            for mode,archive,extra in [('raw',canonical,[offsets]),('zip',zipped,[]),('cache',out/'monolithic.zst',[])]:
                metric=out/(mode+'.bench.json')
                bench=calls.run(name+' '+mode+' bench',[sys.executable,'-B',args.baseline,'bench',mode,archive,query_file,metric,*extra])
                equal_files(expected,Path(str(metric)+'.payloads'))
                measured=json.loads(metric.read_text());validate_metrics(mode,measured,query,lengths,frozen.COST_RECORD_COUNT)
                case['modes'][mode]={'archive':pin(archive),'stored_total_bytes':archive.stat().st_size+(offsets.stat().st_size if mode=='raw' else 0),'pack_native':raw_build if mode=='raw' else zip_build if mode=='zip' else case['modes']['monolithic']['pack_native'],'bench_native':bench,'metrics':measured,'exact_all_records_and_queries':True}
            case['offsets']=pin(offsets);results.append(case)
            (args.output/'PARTIAL_RESULTS.json').write_text(json.dumps(results,indent=2)+'\n')
        after={str(p.resolve()):pin(p) for p in controls};assert before==after
        result.update(status='PASS',source_after=after,fixtures=results,call_count=len(calls.rows))
    except BaseException as error:
        result.update(error=type(error).__name__+': '+str(error),fixtures=results,call_count=len(calls.rows))
        (args.output/'FAILURE.txt').write_text(traceback.format_exc())
    (args.output/'RESULT.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'status':result['status'],'fixtures_completed':len(results),'call_count':len(calls.rows),'error':result.get('error')}))
    return 0 if result['status']=='PASS' else 1

if __name__=='__main__':raise SystemExit(main())
