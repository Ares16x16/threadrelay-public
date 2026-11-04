import { useCallback, useEffect, useRef, useState } from 'react';
import type { Topic } from '../../lib/contracts';
import type { WorkerClient } from '../../lib/useWorker';
import { useResource } from '../../lib/useResource';
import { EmptyState } from '../../components/EmptyState';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { LoadState } from '../common';

type Worker = Pick<WorkerClient,'queryTopics'|'saveTopic'> & Partial<Pick<WorkerClient,'testSample'>>;
const blank = ():Topic => ({id:'',version:1,description:'',positive_examples:[],negative_examples:[],exclusions:[],enabled:true,minimum_score:.9});
export function TopicsView({worker, onDirty, onBusy, onSampleActivity}: {worker:Worker; onDirty?:(dirty:boolean)=>void; onBusy?:(busy:boolean)=>void; onSampleActivity?:(busy:boolean)=>void}) {
  const load = useCallback(()=>worker.queryTopics(),[worker]);
  const data = useResource(load);
  const [draft,setDraft] = useState<Topic|null>(null), [original,setOriginal] = useState<Topic|null>(null), [pending,setPending] = useState(false), [error,setError] = useState(''), [success,setSuccess] = useState(''), [isDirty,setIsDirty] = useState(false), [nextEdit,setNextEdit] = useState<{topic:Topic|null}|null>(null);
  const [sample,setSample]=useState(''),[sampleResult,setSampleResult]=useState(''),[samplePending,setSamplePending]=useState(false);
  const [minimumScore,setMinimumScore]=useState('0.9');
  const mounted=useRef(true), saveLock=useRef(false), busyCallback=useRef(onBusy);busyCallback.current=onBusy;const sampleActivity=useRef(onSampleActivity);sampleActivity.current=onSampleActivity;
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;busyCallback.current?.(false);};},[]);
  const edit = (t:Topic) => { setDraft(t); setOriginal(t); setMinimumScore(String(t.minimum_score??.9)); setError(''); setIsDirty(false); onDirty?.(false); };
  const requestEdit = (t:Topic|null) => { if(saveLock.current)return; if(isDirty){setNextEdit({topic:t});return;} if(t)edit(t);else cancel(); };
  const update = (t:Topic, minimum=minimumScore) => { setDraft(t); const changed=JSON.stringify(t)!==JSON.stringify(original)||minimum!==String(original?.minimum_score??.9); setIsDirty(changed); onDirty?.(changed); };
  const cancel = () => { setDraft(null); setOriginal(null); setError(''); setIsDirty(false); onDirty?.(false); };
  const submit = async () => {
    if(!draft || saveLock.current) return;
    if(!draft.id.trim() || !draft.description.trim()) {setError('Topic ID and description are required.');return;}
    const minimum=minimumScore.trim()?Number(minimumScore):NaN;
    if(!Number.isFinite(minimum)||minimum<.11||minimum>1){setError('Minimum relevance score must be between 0.11 and 1.');return;}
    const exists = !!original?.id;
    if (!exists && data.value?.some(t=>t.id===draft.id.trim())) {setError('A topic with that ID already exists.');return;}
    const saved = {...draft, id:draft.id.trim(), description:draft.description.trim(), minimum_score:minimum, version: exists ? draft.version+1 : 1};
    saveLock.current=true; setPending(true); busyCallback.current?.(true); setError(''); setSuccess('');
    try { await worker.saveTopic(saved); if(!mounted.current)return; cancel(); setSuccess('Topic saved. Saved results keep their earlier definition; reconfirm any Auto routes.'); await data.refresh(); }
    catch { if(mounted.current)setError('Topic save was not confirmed. Your edits are retained; refresh before retrying.'); }
    finally { saveLock.current=false; if(mounted.current){setPending(false);busyCallback.current?.(false);} }
  };
  return <><header className="page-header"><div><h1 tabIndex={-1}>Topics</h1><p>Describe what should be classified locally.</p></div><button className="primary" disabled={pending} onClick={()=>requestEdit(blank())}>Create topic</button></header>
    <p id="relevance-score-help">The model's relevance score is not a probability or measured accuracy. A score at or above a topic's minimum is a match; 0.10 or below is no match. Scores between those cutoffs abstain. A higher minimum requires a stronger model score.</p>
    <LoadState loading={data.loading} error={data.error} hasValue={data.value!==null} retry={()=>void data.refresh()} />
    <div className="split"><section className="panel"><h2>Topic list</h2>{data.value?.length===0 && <EmptyState title="Define what matters">Create a topic with examples to guide local classification.</EmptyState>}{data.value?.map(t=><button className="list-row" disabled={pending} key={t.id} onClick={()=>requestEdit(t)}><strong>{t.id}</strong><span>{t.description}</span><small>{t.enabled?'Enabled':'Disabled'} · v{t.version} · Minimum score {t.minimum_score??.9}</small></button>)}</section>
      <section className="panel"><h2>{draft ? 'Topic editor' : 'Select a topic'}</h2>{draft ? <form onSubmit={e=>{e.preventDefault();void submit();}}><fieldset disabled={pending} className="editor-fields">
        <label>Topic ID<input value={draft.id} maxLength={128} disabled={!!data.value?.some(t=>t.id===original?.id)} onChange={e=>update({...draft,id:e.target.value})} aria-invalid={!!error&&!draft.id.trim()} /></label>
        <label>Description<textarea value={draft.description} maxLength={10000} onChange={e=>update({...draft,description:e.target.value})} aria-invalid={!!error&&!draft.description.trim()} /></label>
        <label>Minimum relevance score<input type="number" required min={.11} max={1} step="any" value={minimumScore} aria-describedby="relevance-score-help" onChange={e=>{setMinimumScore(e.target.value);update(draft,e.target.value);}} /></label>
        {(['positive_examples','negative_examples','exclusions'] as const).map((key,i)=><label key={key}>{['Positive examples','Negative examples','Exclusions'][i]}<textarea value={draft[key].join('\n')} onChange={e=>update({...draft,[key]:e.target.value.split('\n').slice(0,100)})} /><small>One example per line, up to 100.</small></label>)}
        <label className="check"><input type="checkbox" role="switch" className="switch-control" checked={draft.enabled} onChange={e=>update({...draft,enabled:e.target.checked})} /> Enabled</label>
        {error && <p role="alert" className="error">{error}</p>}<div className="actions"><button className="primary" disabled={pending || !!data.error}>{pending?'Saving…':'Save topic'}</button><button type="button" onClick={()=>requestEdit(null)}>Cancel</button></div>
      </fieldset></form> : <p className="muted">Choose a topic to edit, or create one.</p>}</section></div>
    {success && <p role="status">{success}</p>}
    {nextEdit && <ConfirmDialog title="Discard topic edits?" confirmLabel="Discard edits" onCancel={()=>setNextEdit(null)} onConfirm={()=>{const next=nextEdit.topic;setNextEdit(null);if(next)edit(next);else cancel();}}><p>Unsaved topic edits will be discarded.</p></ConfirmDialog>}
    <section className="panel"><h2>Test sample</h2><p>Stop collection first. This isolated local classification uses saved topics. Its exact sample input, topic definitions and model-run result are saved permanently in History. It does not create a delivery approval, queue or send messages. A verified owned model is required.</p><form onSubmit={e=>{e.preventDefault();if(!worker.testSample||samplePending)return;setSamplePending(true);sampleActivity.current?.(true);setSampleResult('');void worker.testSample(sample).then(result=>{if(mounted.current)setSampleResult(result.classification.topic_results.map(t=>`${t.topic_id}: ${t.abstained?'Abstained':t.match?'Match':'No match'}${typeof t.score==='number'&&Number.isFinite(t.score)?` · relevance score ${t.score}`:''}`).join('; ')||'No enabled topics');}).catch(()=>{if(mounted.current)setSampleResult('Sample unavailable. Stop collection and verify the local model.');}).finally(()=>{sampleActivity.current?.(false);if(mounted.current)setSamplePending(false);});}}><label>Sample text<textarea required maxLength={16000} value={sample} onChange={e=>setSample(e.target.value)} /></label><button disabled={samplePending||!worker.testSample}>{samplePending?'Classifying…':'Classify sample without sending'}</button></form>{sampleResult&&<p role="status">{sampleResult}</p>}</section>
  </>;
}
