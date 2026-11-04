import { useCallback, useRef, useState } from 'react';
import type { WorkerClient } from '../../lib/useWorker';
import { useResource } from '../../lib/useResource';
import { LoadState } from '../common';

/** Observational local results. This component never obtains account or send authority. */
export function CollectedPosts({worker}: {worker:Pick<WorkerClient,'queryCollection'>&Partial<Pick<WorkerClient,'testSample'>>}) {
  const load=useCallback(()=>worker.queryCollection(),[worker]);
  const data=useResource(load,5000);
  const [topic,setTopic]=useState(''),[result,setResult]=useState('match');
  const [classifying,setClassifying]=useState<string|null>(null),[runMessage,setRunMessage]=useState(''),runLock=useRef(false);
  const reclassify=async(post:import('../../lib/contracts').ObservedPost)=>{
    if(!worker.testSample||runLock.current)return;
    runLock.current=true;setClassifying(post.post_id);setRunMessage('Classifying saved post…');
    try{await worker.testSample(post.text,post.post_id);setRunMessage('Result updated with current topics.');await data.refresh({background:true});}
    catch{setRunMessage('Could not reclassify. Stop collection and check that the saved post is still available, then try again.');}
    finally{runLock.current=false;setClassifying(null);}
  };
  const rows=data.value??[];
  const topics=new Map(rows.flatMap(row=>row.topics.map(value=>[value.id,value.label] as const)));
  const selectedTopic=topics.has(topic)?topic:'';
  const topicRows=rows.map(row=>({row,results:row.classification?.topic_results.filter(value=>!selectedTopic||value.topic_id===selectedTopic)??[]}))
    .filter(({results})=>!selectedTopic||results.length);
  const counts={match:0,abstained:0,noMatch:0,needsClassification:0};
  for(const {results} of topicRows){
    if(results.some(value=>value.match&&!value.abstained))counts.match++;
    else if(results.some(value=>value.abstained))counts.abstained++;
    else if(results.length)counts.noMatch++;
    else counts.needsClassification++;
  }
  const selected=topicRows.filter(({results})=>{
    return result==='all'||results.some(value=>result==='match' ? value.match&&!value.abstained : value.abstained);
  }).map(({row})=>row);
  return <section className="panel" aria-labelledby="collected-posts-heading">
    <div className="section-heading"><h2 id="collected-posts-heading">Collected posts</h2><button disabled={data.loading} onClick={()=>void data.refresh()}>Refresh collected posts</button></div>
    <p>Matches use your current topics. All captured posts and model runs remain in History.</p>
    <details><summary>About these results</summary><p>All topics shows a match to any current topic. Older topic results remain in permanent History. Changed posts or topics need a new classification. Counts group each post by match, then abstained, then no match.</p><p>No Telegram account or delivery route is needed. These results do not approve or send messages. Relevance scores are model scores, not probabilities.</p></details>
    <LoadState loading={data.loading} error={data.error} hasValue={data.value!==null} retry={()=>void data.refresh()} />
    {!!rows.length&&<div className="actions">
      <label>Topic filter<select value={selectedTopic} onChange={event=>setTopic(event.target.value)}><option value="">All topics</option>{[...topics].map(([id,label])=><option key={id} value={id}>{label}</option>)}</select></label>
      <label>Result filter<select value={result} onChange={event=>setResult(event.target.value)}><option value="all">All results</option><option value="match">Matches</option><option value="abstained">Abstained</option></select></label>
    </div>}
    {data.value!==null&&<><p className="muted" aria-live="polite">{data.error||data.loading?'Last loaded results: ':''}Matches {counts.match} · Abstained {counts.abstained} · No match {counts.noMatch} · Needs classification {counts.needsClassification}</p><p className="muted">Showing {selected.length} of {rows.length} recent retained posts. This view includes up to 100 posts; long posts may reduce the number shown.</p></>}
    {data.value?.length===0&&<p>No collected posts yet. Save an enabled topic, then open Threads and start a Review or Dry run session to collect public posts.</p>}
    {!!rows.length&&!selected.length&&<><p>No collected posts match these filters.</p>{result==='match'&&<button onClick={()=>setResult('all')}>Show all results</button>}</>}
    {runMessage&&<p role="status">{runMessage}</p>}
    {selected.map(row=><article key={row.post.post_id} aria-label={`Collected post by ${row.post.author}`}>
      <h3>{row.post.author}</h3><p className="preserve">{row.post.text}</p>
      {row.post.quote&&<blockquote className="preserve">Quoted from {row.post.quote.author}: {row.post.quote.text}</blockquote>}
      <p className="muted">Source: {row.post.permalink}</p>
      <p className="muted">Observed: {row.post.observed_at} · Visibility: {row.post.visibility} · Quality: {row.post.quality_flags.map(flag=>flag.code).join(', ')||'no flagged issue'}</p>
      {row.classification ? <><ul>{row.classification.topic_results.map(value=><li key={value.topic_id}><span>{row.topics.find(entry=>entry.id===value.topic_id)?.label??value.topic_id}: {value.abstained?'Abstained':value.match?'Match':'No match'}</span>{typeof value.score==='number'&&Number.isFinite(value.score)&&<span> · relevance score {value.score}</span>}</li>)}</ul>{!row.classification.topic_results.length&&<p>No enabled topics were classified.</p>}<p className="muted">Model: {row.classification.model_version}</p></> : <p className="notice">Classification unavailable or changed. Collect this post again with your current topics to obtain a new result.</p>}
      {worker.testSample&&row.post.text.trim()&&<button disabled={classifying!==null||data.loading||!!data.error} onClick={()=>void reclassify(row.post)}>{classifying===row.post.post_id?'Classifying…':'Reclassify with current topics'}</button>}
    </article>)}
  </section>;
}
