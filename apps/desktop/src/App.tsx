import { useCallback, useEffect, useRef, useState } from 'react';
import type { SessionConfig, SessionSnapshot } from './lib/contracts';
import { useWorker } from './lib/useWorker';
import { SecureAuthClient } from './lib/secureAuth';
import { EmbeddedThreadsClient } from './lib/embeddedThreads';
import { EmbeddedThreadsPane, type EmbeddedThreadsHandle } from './features/threads/EmbeddedThreadsPane';
import { ConfirmDialog } from './components/ConfirmDialog';
import { DashboardView, DEFAULT_CONFIG } from './features/dashboard/View';
import { InboxView } from './features/inbox/View';
import { TopicsView } from './features/topics/View';
import { RoutesView } from './features/routes/View';
import { HistoryView } from './features/history/View';
import { AccountsView } from './features/accounts/View';
import { SettingsView } from './features/settings/View';
import { AppStatusBar } from './components/AppStatusBar';
import { ActivityMonitor, ActivityView } from './features/activity/View';
import './styles/tokens.css';
import './App.css';

const views = ['Dashboard','Threads','Live activity','Review inbox','Topics','Routes','History','Accounts','Settings'] as const;
type View = typeof views[number];
const navigation: { label: string; items: readonly View[] }[] = [
  { label: 'Workspace', items: ['Dashboard', 'Threads', 'Live activity'] },
  { label: 'Library', items: ['Review inbox', 'History'] },
  { label: 'Configuration', items: ['Topics', 'Routes', 'Accounts', 'Settings'] },
];
export default function App() {
  const worker=useWorker(), secure=useRef(new SecureAuthClient()).current, [view,setView]=useState<View>('Dashboard'), [theme,setTheme]=useState('system'), [dirty,setDirty]=useState(false), [next,setNext]=useState<View|null>(null), [session,setSession]=useState<SessionSnapshot|null>(null), [busy,setBusy]=useState(false);
  const main=useRef<HTMLElement>(null), navigated=useRef(false);
  const [connectionRevision,setConnectionRevision]=useState(0),[samplePending,setSamplePending]=useState(false);
  const [controlRevision,setControlRevision]=useState(0),[startPending,setStartPending]=useState(false),[resuming,setResuming]=useState(false),[resumeRevision,setResumeRevision]=useState(0);
  const collectionIntent=useRef(0),lastConfig=useRef<SessionConfig>(DEFAULT_CONFIG);
  const resumeRequest=useRef<{ticket:number;config:SessionConfig;dispatched:boolean;resolve:(snapshot:SessionSnapshot)=>void;reject:(error:Error)=>void}|null>(null);
  const latestSession=useRef(session);latestSession.current=session;
  const acceptState=useCallback((snapshot:SessionSnapshot|null)=>{latestSession.current=snapshot;if(snapshot?.config)lastConfig.current=snapshot.config;setSession(snapshot);},[]);
  const threads=useRef(new EmbeddedThreadsClient()).current,threadsGeneration=useRef<number|null>(null),navigationLock=useRef(false),[closingThreads,setClosingThreads]=useState(false),[navigationError,setNavigationError]=useState(''),[threadsMounted,setThreadsMounted]=useState(false);
  const threadsHandle=useRef<EmbeddedThreadsHandle|null>(null);
  const cancelQueuedStart=()=>{collectionIntent.current++;const request=resumeRequest.current;resumeRequest.current=null;request?.reject(new Error('Collection resume cancelled'));setResuming(false);setStartPending(false);setControlRevision(value=>value+1);};
  useEffect(()=>()=>{collectionIntent.current++;resumeRequest.current?.reject(new Error('App closed'));resumeRequest.current=null;},[]);
  useEffect(()=>{if(navigated.current)Array.from(main.current?.querySelectorAll('h1')??[]).find(heading=>!heading.closest('[hidden]'))?.focus();},[view]);
  const promote=(target:View)=>{if(target==='Threads')setThreadsMounted(true);navigated.current=true;setView(target);};
  const ensureForStart=async()=>{const ticket=collectionIntent.current;const handle=threadsHandle.current;if(!handle?.ensureOpen)throw new Error('Threads visibility unavailable');await handle.ensureOpen();if(ticket!==collectionIntent.current)throw new Error('Collection start cancelled');};
  const resume=()=>new Promise<SessionSnapshot>((resolve,reject)=>{
    if(dirty||busy||navigationLock.current||resumeRequest.current||latestSession.current?.state!=='paused'){reject(new Error('Collection cannot resume here'));return;}
    const ticket=++collectionIntent.current;resumeRequest.current={ticket,config:{...lastConfig.current},dispatched:false,resolve,reject};setControlRevision(value=>value+1);setResuming(true);setResumeRevision(value=>value+1);promote('Threads');
  });
  useEffect(()=>{
    const request=resumeRequest.current;if(view!=='Threads'||!request||request.dispatched)return;request.dispatched=true;
    void(async()=>{
      try{await ensureForStart();if(request.ticket!==collectionIntent.current)throw new Error('Collection resume cancelled');const next=await worker.start(request.config);if(request.ticket!==collectionIntent.current)throw new Error('Collection resume cancelled');acceptState(next);request.resolve(next);}
      catch(error){request.reject(error instanceof Error?error:new Error('Collection resume unavailable'));}
      finally{if(resumeRequest.current===request){resumeRequest.current=null;setControlRevision(value=>value+1);setResuming(false);}}
    })();
  },[view,resumeRevision,worker,acceptState]);
  const navigate=(target:View)=>{
    if(busy||navigationLock.current||startPending||resuming||session?.state==='starting'||target===view)return;
    const finish=()=>{setNavigationError('');if(dirty){setNext(target);return;}promote(target);};
    const current=threadsGeneration.current;
    if(view==='Threads'&&current!==null){
      navigationLock.current=true;setClosingThreads(true);
      const handle=threadsHandle.current;
      const hiding=handle?handle.hide({preserveCapture:latestSession.current?.state==='running'}):Promise.reject(new Error('Browser visibility unavailable'));
      void hiding.then(()=>{
        if(threadsGeneration.current!==current)throw new Error('Threads generation changed');finish();
      }).catch(()=>setNavigationError('Hide Threads could not be confirmed. Stay in this view and retry navigation or Close Threads.'))
        .finally(()=>{navigationLock.current=false;setClosingThreads(false);});
    }else finish();
  };
  return <div className="app" data-theme={theme}><a className="skip" href="#main">Skip to content</a><aside className="sidebar"><div className="brand"><span className="brand-mark" aria-hidden="true"><i></i><i></i><i></i></span><div><strong>ThreadRelay</strong><span>Local workspace</span></div></div><nav aria-label="Main navigation">{navigation.map(group=><div className="nav-group" key={group.label}><div className="nav-label">{group.label}</div>{group.items.map(v=><button key={v} disabled={busy||closingThreads||startPending||resuming||session?.state==='starting'} aria-current={view===v?'page':undefined} onClick={()=>navigate(v)}>{v}</button>)}</div>)}</nav><div className="sidebar-foot">Your workspace runs locally.</div></aside>
    <div className="workspace"><AppStatusBar worker={worker} samplePending={samplePending} onState={acceptState} onBeforeControl={cancelQueuedStart} onResume={resume} preparing={startPending||resuming} resumeDisabled={dirty||busy||closingThreads} onReconnect={snapshot=>{acceptState(snapshot);setConnectionRevision(value=>value+1);}}/>{navigationError&&<p role="alert" className="error">{navigationError}</p>}<main id="main" ref={main} tabIndex={-1}>
      {view==='Dashboard' && <DashboardView connectionManaged controlRevision={controlRevision} refreshKey={connectionRevision} observeInitial navigate={navigate} worker={worker} onState={acceptState} initialSnapshot={session} />}
      {threadsMounted && <div className="persistent-threads" hidden={view!=='Threads'}><DashboardView connectionManaged controlRevision={controlRevision} onStartPending={setStartPending} refreshKey={connectionRevision} compact observeInitial navigate={navigate} worker={worker} onState={acceptState} initialSnapshot={session} beforeStart={ensureForStart}><div className="threads-live-grid"><EmbeddedThreadsPane client={threads} visible={view==='Threads'} collecting={session?.state==='running'} externalBusy={closingThreads} onBusy={setBusy} onHandle={value=>{threadsHandle.current=value;}} onGeneration={value=>{threadsGeneration.current=value;}}/><ActivityMonitor compact enabled={view==='Threads'} worker={worker} onOpenHistory={()=>navigate('History')}/></div></DashboardView></div>}
      {view==='Live activity' && <ActivityView worker={worker} onOpenHistory={()=>navigate('History')}/>}
      {view==='Review inbox' && <InboxView worker={worker} navigate={()=>navigate('Dashboard')} />}
      {view==='Topics' && <TopicsView worker={worker} onDirty={setDirty} onBusy={setBusy} onSampleActivity={setSamplePending} />}
      {view==='Routes' && <RoutesView worker={worker} onDirty={setDirty} onBusy={setBusy} onOpenAccounts={()=>navigate('Accounts')} />}
      {view==='History' && <HistoryView worker={worker} onOpenAccounts={()=>navigate('Accounts')} onBusy={setBusy} />}
      {view==='Accounts' && <AccountsView worker={worker} secure={secure} onOpenThreads={()=>navigate('Threads')} onOpenRoutes={()=>navigate('Routes')} />}
      {view==='Settings' && <SettingsView worker={worker} theme={theme} onTheme={setTheme} />}
    </main></div>
    {next && <ConfirmDialog title="Discard unsaved changes?" confirmLabel="Discard changes" onCancel={()=>setNext(null)} onConfirm={()=>{setDirty(false);promote(next);setNext(null);}}><p>Your unsaved non-secret form edits will be discarded.</p></ConfirmDialog>}
  </div>;
}
