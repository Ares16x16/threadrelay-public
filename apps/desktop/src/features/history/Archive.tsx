import { useCallback, useState } from 'react';
import type { Classification, ObservedPost } from '../../lib/contracts';
import { useDetailHeading } from './useDetailHeading';
import { useResource } from '../../lib/useResource';
import { LoadState } from '../common';
import type { ArchiveClient, RunDetail } from './archiveTypes';

const percent=new Intl.NumberFormat('en-GB',{style:'percent',maximumFractionDigits:1});
function scoreLabel(score:number|null|undefined) {
 return typeof score==='number'&&Number.isFinite(score)&&score>=0&&score<=1?`Model score ${percent.format(score)}`:'Score unavailable';
}
function ScoreSummary({classification}:{classification:Classification|null}) {
 return classification?<><ul aria-label="Saved classification scores">{classification.topic_results.map(result=><li key={result.topic_id}>{result.topic_id}: {result.abstained?'Abstained':result.match?'Match':'No match'} · {scoreLabel(result.score)}</li>)}</ul><p className="muted">Model scores measure relevance, not calibrated probabilities.</p></>:null;
}
export function RecordFields({value}:{value:Record<string,unknown>}) {
 return <dl className="details">{Object.entries(value).map(([key,item])=><div key={key}><dt>{key.replaceAll('_',' ')}</dt><dd>{item===null||item===undefined?'Not recorded':typeof item==='object'?Array.isArray(item)?item.length?<ul>{item.map((v,i)=><li key={i}>{typeof v==='object'&&v!==null?<RecordFields value={v as Record<string,unknown>}/>:String(v)}</li>)}</ul>:'None':<RecordFields value={item as Record<string,unknown>}/>:String(item)}</dd></div>)}</dl>;
}
export function PostContent({post}:{post:ObservedPost}) {
 const {text,quote,quality_flags,...metadata}=post;
 return <><p className="preserve">{text||'No own text was captured.'}</p>{quote&&<blockquote className="preserve">Quoted from {quote.author}: {quote.text}{quote.permalink&&<p>{quote.permalink}</p>}</blockquote>}<h3>Captured metadata</h3><RecordFields value={metadata}/><h3>Extraction quality</h3>{quality_flags.length?<ul>{quality_flags.map((flag,i)=><li key={i}>{flag.code}: {flag.detail}</li>)}</ul>:<p>No flagged extraction issue.</p>}<p className="muted">Only observed values are shown. Missing publication, engagement or media metadata was not inferred.</p></>;
}
function Pager({kind,previous,next,busy,onPrevious,onNext}:{kind:string;previous:boolean;next:boolean;busy:boolean;onPrevious:()=>void;onNext:()=>void}) {
 return <div className="actions"><button disabled={busy||!previous} onClick={onPrevious}>Previous {kind} page</button><button disabled={busy||!next} onClick={onNext}>Next {kind} page</button></div>;
}
function PostDetail({worker,id,onRuns,onPrepare}:{worker:ArchiveClient;id:string;onRuns:(id:string)=>void;onPrepare:(ids:string[])=>void}) {
 const heading=useDetailHeading();
 const load=useCallback(()=>worker.queryArchiveRevision(id),[worker,id]),data=useResource(load);
 const loadRun=useCallback(async()=>{
  const page=await worker.queryArchiveRuns({revision_id:id,limit:1});
  return page.items[0]?worker.queryArchiveRun(page.items[0].run_id):null;
 },[worker,id]),latestRun=useResource(loadRun);
 return <section className="panel" aria-label="Post details"><h2 tabIndex={-1} ref={heading}>Post details</h2><LoadState loading={data.loading} error={data.error} hasValue={false} retry={()=>void data.refresh()}/>{!data.loading&&!data.error&&data.value&&<><PostContent post={data.value}/><h3>Latest saved model run</h3><LoadState loading={latestRun.loading} error={latestRun.error} hasValue={false} retry={()=>void latestRun.refresh()}/>{!latestRun.loading&&!latestRun.error&&(latestRun.value?<Results run={latestRun.value}/>:<p>No model run recorded for this revision.</p>)}<div className="actions"><button onClick={()=>onRuns(id)}>View model runs for this revision</button><button onClick={()=>onPrepare([id])}>Prepare send from this revision</button></div></>}</section>;
}
export function ArchivePosts({worker,onRuns,onPrepare}:{worker:ArchiveClient;onRuns:(id:string)=>void;onPrepare:(ids:string[])=>void}) {
 const [text,setText]=useState(''),[search,setSearch]=useState(''),[cursors,setCursors]=useState<Array<string|null>>([null]),[detail,setDetail]=useState<string|null>(null),[selected,setSelected]=useState<string[]>([]);
 const cursor=cursors[cursors.length-1],load=useCallback(()=>worker.queryArchivePosts({cursor,limit:20,search}),[worker,cursor,search]),data=useResource(load);
 return <><section className="panel"><div className="section-heading"><h2>Archived posts</h2><button disabled={data.loading} onClick={()=>void data.refresh()}>Refresh posts</button></div><p>One latest captured revision per post, independent of Telegram. Search uses the displayed revision. Earlier revisions and model runs remain saved. Inspect a post for its exact content.</p>
 <form onSubmit={event=>{event.preventDefault();setSearch(text.trim());setCursors([null]);setDetail(null);}}><label>Search archived posts<input maxLength={200} value={text} onChange={event=>setText(event.target.value)}/></label><button>Search posts</button></form>
 <div className="actions"><span>{selected.length} of 50 sources selected</span><button disabled={!selected.length} onClick={()=>onPrepare(selected)}>Prepare send from selected posts</button><button disabled={!selected.length} onClick={()=>setSelected([])}>Clear selection</button></div>
 <LoadState loading={data.loading} error={data.error} hasValue={false} retry={()=>void data.refresh()}/>
 {!data.loading&&!data.error&&data.value&&<>{!data.value.items.length&&<p>No archived posts match this search. Collect visible public posts from Threads to add history.</p>}{data.value.items.map(post=><article key={post.revision_id}><label className="check"><input type="checkbox" aria-label={`Select revision ${post.revision_id}`} checked={selected.includes(post.revision_id)} disabled={!selected.includes(post.revision_id)&&selected.length>=50} onChange={event=>setSelected(old=>event.target.checked?[...old,post.revision_id]:old.filter(id=>id!==post.revision_id))}/><strong>{post.author}</strong></label><p className="preserve">{post.text_preview}</p><p className="muted">Observed {post.observed_at} · {post.visibility} · {post.provenance==='legacy_import'?'Imported from earlier retained data':'Captured observation'}</p><p className="wrap">{post.permalink}</p><button onClick={()=>setDetail(post.revision_id)}>Inspect post by {post.author}</button></article>)}</>}
 <Pager kind="posts" previous={cursors.length>1} next={!!data.value?.next_cursor&&!data.error} busy={data.loading} onPrevious={()=>setCursors(old=>old.slice(0,-1))} onNext={()=>{if(data.value?.next_cursor)setCursors(old=>[...old,data.value!.next_cursor]);}}/></section>
 {detail&&<PostDetail key={detail} worker={worker} id={detail} onRuns={onRuns} onPrepare={onPrepare}/>}</>;
}
function Results({run}:{run:RunDetail}) {
 const topics=new Map(run.topics.map(topic=>[topic.id,topic]));
 return run.classification?<><h3>Historical classification</h3><p>Model: {run.classification.model_version}</p><ul>{run.classification.topic_results.map(result=>{
  const topic=topics.get(result.topic_id),threshold=topic?.minimum_score??run.binding.positive_threshold;
  const recordedThreshold=typeof threshold==='number'&&Number.isFinite(threshold)&&threshold>=0&&threshold<=1?threshold:null;
  return <li key={result.topic_id}>{result.topic_id}: {result.abstained?'Abstained':result.match?'Match':'No match'} · {topic?`saved topic v${topic.version}`:'topic version not recorded'} · {recordedThreshold===null?'minimum score not recorded':`minimum score ${recordedThreshold}`} · {scoreLabel(result.score)}</li>;
 })}</ul><p className="muted">Editing Topics does not reclassify saved runs. Each result uses its saved definition and threshold.</p><p className="muted">Scores are not calibrated probabilities. Historical matches do not grant current send authority.</p></>:<p>No successful classification recorded.</p>;
}
function RunDetails({worker,id,onPrepare}:{worker:ArchiveClient;id:string;onPrepare:(ids:string[])=>void}) {
 const heading=useDetailHeading();
 const load=useCallback(()=>worker.queryArchiveRun(id),[worker,id]),data=useResource(load),run=data.value;
 return <section className="panel" aria-label="Model run details"><h2 tabIndex={-1} ref={heading}>Model run details</h2><LoadState loading={data.loading} error={data.error} hasValue={false} retry={()=>void data.refresh()}/>{!data.loading&&!data.error&&run&&<><p>{run.kind} · {run.status} · {run.provenance==='legacy_import'?'Legacy record; unavailable provenance is not reconstructed.':'Captured run'}</p><p>Started: {run.started_at??'Not recorded'} · Finished: {run.finished_at??'Not recorded'}</p>{run.error_code&&<p className="notice">Result reason: {run.error_code}</p>}<h3>Saved input</h3>{run.input_snapshot.post?<PostContent post={run.input_snapshot.post}/>:<p className="preserve">{run.input_snapshot.text??'Input not recorded.'}</p>}<Results run={run}/><h3>Topic definitions at execution</h3>{run.topics.length?run.topics.map(topic=><details key={topic.id}><summary>{topic.description} · version {topic.version}</summary><RecordFields value={{...topic}}/></details>):<p>Topic definitions were not recorded.</p>}<h3>Model and runtime bindings</h3>{Object.keys(run.binding).length?<RecordFields value={run.binding}/>:<p>Bindings were not recorded.</p>}{run.kind==='collection'&&run.revision_id&&<div className="actions"><button onClick={()=>onPrepare([run.revision_id!])}>Prepare send from source revision</button></div>}</>}</section>;
}
export function ArchiveRuns({worker,revisionId,onClear,onPrepare}:{worker:ArchiveClient;revisionId:string|null;onClear:()=>void;onPrepare:(ids:string[])=>void}) {
 const [cursors,setCursors]=useState<Array<string|null>>([null]),[detail,setDetail]=useState<string|null>(null),cursor=cursors[cursors.length-1];
 const load=useCallback(()=>worker.queryArchiveRuns({cursor,limit:20,revision_id:revisionId}),[worker,cursor,revisionId]),data=useResource(load);
 return <><section className="panel"><div className="section-heading"><h2>Saved model runs</h2><button disabled={data.loading} onClick={()=>void data.refresh()}>Refresh model runs</button></div><p>Inputs, topic versions and results are saved permanently. Cancelled or failed runs do not authorize delivery.</p>{revisionId&&<div className="actions"><span className="wrap">Source revision: {revisionId}</span><button onClick={onClear}>Show all model runs</button></div>}<LoadState loading={data.loading} error={data.error} hasValue={false} retry={()=>void data.refresh()}/>{!data.loading&&!data.error&&data.value&&<>{!data.value.items.length&&<p>No saved model runs for this selection.</p>}{data.value.items.map(run=><article key={run.run_id}><h3>{run.kind==='sample'?'Saved sample':run.kind==='legacy'?'Imported result':'Collected post'} · <span>{run.status[0].toUpperCase()+run.status.slice(1)}</span></h3><p className="preserve">{run.input_preview}</p><p className="muted">{run.started_at??'Execution time not recorded'} · {run.topic_count} topic definitions</p>{run.error_code&&<p>{run.error_code}</p>}<ScoreSummary classification={run.classification}/><button onClick={()=>setDetail(run.run_id)}>Inspect run {run.run_id}</button></article>)}</>}<Pager kind="model runs" previous={cursors.length>1} next={!!data.value?.next_cursor&&!data.error} busy={data.loading} onPrevious={()=>setCursors(old=>old.slice(0,-1))} onNext={()=>{if(data.value?.next_cursor)setCursors(old=>[...old,data.value!.next_cursor]);}}/></section>{detail&&<RunDetails key={detail} worker={worker} id={detail} onPrepare={onPrepare}/>}</>;
}
