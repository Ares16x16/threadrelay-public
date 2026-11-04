import { useCallback, useEffect, useRef, useState } from 'react';
import { EmbeddedThreadsClient, type EmbeddedBounds } from '../../lib/embeddedThreads';

type Client=Pick<EmbeddedThreadsClient,'open'|'resize'|'hide'|'background'|'show'|'close'|'status'>;
const nativeClient=new EmbeddedThreadsClient();
function visibleBounds(node:HTMLElement):EmbeddedBounds {
  const rect=node.getBoundingClientRect();
  const clip=node.closest('main')?.getBoundingClientRect();
  // CSS overflow does not clip a native child webview. Keep it inside the
  // independently scrolling main pane, below fixed navigation and status.
  const x=Math.max(0,rect.left,clip?.left??0),y=Math.max(0,rect.top,clip?.top??0);
  const width=Math.min(window.innerWidth,rect.right,clip?.right??window.innerWidth)-x;
  const height=Math.min(window.innerHeight,rect.bottom,clip?.bottom??window.innerHeight)-y;
  if(width<1||height<1)throw new Error('Viewport is not visible');
  return {x,y,width,height};
}

export type EmbeddedThreadsHandle={hide:(options?:{preserveCapture?:boolean})=>Promise<void>;ensureOpen?:()=>Promise<void>};
export function EmbeddedThreadsPane({client=nativeClient,onBusy,onGeneration,onHandle,onSuspendCollection,externalBusy=false,visible=true,collecting=false}: {client?:Client;onBusy?:(busy:boolean)=>void;onGeneration?:(value:number|null)=>void;onHandle?:(value:EmbeddedThreadsHandle|null)=>void;onSuspendCollection?:()=>Promise<void>;externalBusy?:boolean;visible?:boolean;collecting?:boolean}) {
  const viewport=useRef<HTMLDivElement>(null),active=useRef(false),current=useRef<number|null>(null),locked=useRef(false),hidden=useRef(false),recover=useRef(false),visibilityBusy=useRef(false),intent=useRef(0),hidePending=useRef<Promise<void>|null>(null);
  const callbacks=useRef({onBusy,onGeneration,onHandle,onSuspendCollection});callbacks.current={onBusy,onGeneration,onHandle,onSuspendCollection};
  const visibleRef=useRef(visible);visibleRef.current=visible;const observedVisibility=useRef(visible);
  const collectingRef=useRef(collecting);collectingRef.current=collecting;const hidePreserves=useRef(false);
  const externalBusyRef=useRef(externalBusy);externalBusyRef.current=externalBusy;
  const [opened,setOpened]=useState(false),[shown,setShown]=useState(false),[pending,setPending]=useState(false),[error,setError]=useState(''),[backgrounded,setBackgrounded]=useState(false);
  useEffect(()=>{active.current=true;return()=>{active.current=false;intent.current++;const owned=current.current;current.current=null;if(owned!==null)void client.close(owned).catch(()=>{});callbacks.current.onBusy?.(false);};},[client]);
  const hide=useCallback(async(reportError=true,restoreWhenVisible=false,preserveCapture=false)=>{
    // An explicit navigation hide cancels any automatic layout recovery,
    // including a hide already waiting for its native acknowledgment.
    if(!restoreWhenVisible)recover.current=false;
    if(hidePending.current&&(!hidePreserves.current||preserveCapture))return hidePending.current;
    const owned=current.current;if(owned===null)return;
    recover.current=restoreWhenVisible;hidePreserves.current=preserveCapture;
    const revision=++intent.current;hidden.current=true;visibilityBusy.current=true;setShown(false);setPending(true);callbacks.current.onBusy?.(true);
    const operation=(async()=>{try{await (preserveCapture?client.background(owned):client.hide(owned));if(current.current!==owned||intent.current!==revision)throw new Error('Browser changed');if(active.current)setBackgrounded(preserveCapture);}
      catch{if(reportError&&active.current&&intent.current===revision)setError('Hiding Threads is unconfirmed. Retry navigation or close Threads.');throw new Error('Hide Threads unconfirmed');}
      finally{if(intent.current===revision){hidePending.current=null;visibilityBusy.current=false;callbacks.current.onBusy?.(false);if(active.current)setPending(false);}}})();
    hidePending.current=operation;return operation;
  },[client]);
  const show=useCallback(async(reveal=true)=>{
    const owned=current.current;if(owned===null||!viewport.current)return;
    const revision=++intent.current;hidePending.current=null;recover.current=false;hidden.current=false;visibilityBusy.current=true;setShown(false);setPending(true);setError('');callbacks.current.onBusy?.(true);
    try{if(reveal)viewport.current.scrollIntoView?.({block:'nearest'});await client.show(owned,visibleBounds(viewport.current));if(active.current&&current.current===owned&&intent.current===revision&&visibleRef.current){setShown(true);setBackgrounded(false);}}
    catch{if(active.current&&intent.current===revision){hidden.current=true;recover.current=visibleRef.current;setError('Threads could not be shown. Retry Show Threads or close it.');}}
    finally{if(intent.current===revision){visibilityBusy.current=false;callbacks.current.onBusy?.(false);if(active.current)setPending(false);}}
  },[client]);
  useEffect(()=>{
    const becameVisible=visible&&!observedVisibility.current;observedVisibility.current=visible;
    if(!opened)return;
    if(!visible&&!hidden.current)void hide(true,false,collectingRef.current).catch(()=>{});
    // Opening publishes state after the native callback. Its first passive
    // effect must not countermand an explicit hide already awaiting navigation.
    // Only a new false-to-true page transition can request an automatic Show.
    else if(becameVisible&&hidden.current)void show();
  },[visible,opened,hide,show]);
  const open=async()=>{
    if(externalBusy||locked.current||current.current!==null||!viewport.current)return;
    locked.current=true;setPending(true);setError('');callbacks.current.onBusy?.(true);
    try {
      viewport.current.scrollIntoView?.({block:'nearest'});
      const result=await client.open(visibleBounds(viewport.current));
      if(!active.current){await client.close(result.generation);return;}
      current.current=result.generation;hidden.current=false;recover.current=false;setOpened(true);setShown(true);callbacks.current.onGeneration?.(result.generation);
      viewport.current?.scrollIntoView?.({block:'nearest'});
    } catch {if(active.current)setError('Threads could not open in this window. Keep the viewport visible and retry.');}
    finally{locked.current=false;callbacks.current.onBusy?.(false);if(active.current)setPending(false);}
  };
  useEffect(()=>{callbacks.current.onHandle?.({hide:options=>hide(false,false,options?.preserveCapture===true),ensureOpen:async()=>{
    if(!visibleRef.current||locked.current||externalBusyRef.current)throw new Error('Threads is not available for collection');
    if(current.current===null)await open();
    if(current.current===null)throw new Error('Threads could not open');
    await show();
    if(hidden.current||!visibleRef.current)throw new Error('Threads is not visible');
  }});return()=>callbacks.current.onHandle?.(null);});
  const close=async()=>{
    const owned=current.current;if(externalBusy||locked.current||owned===null)return;
    locked.current=true;intent.current++;hidden.current=true;recover.current=false;setShown(false);setPending(true);setError('');callbacks.current.onBusy?.(true);
    try{await client.close(owned);if(current.current===owned){current.current=null;callbacks.current.onGeneration?.(null);if(active.current)setOpened(false);}}
    catch{if(active.current)setError('Closing Threads is unconfirmed. Retry Close Threads.');}
    finally{locked.current=false;callbacks.current.onBusy?.(false);if(active.current)setPending(false);}
  };
  useEffect(()=>{
    if(!opened||!visible)return;
    let live=true,resizing=false,queued=false,checking=false,frame=0,nextRecovery=0,statusFailures=0,lastBounds:EmbeddedBounds|null=null;
    const statusNotice='Checking the Threads browser connection again. Your page has been kept open.';
    const fail=async(owned:number)=>{if(!live||hidden.current||current.current!==owned)return;
      // Cancelling collection must not delay the native hide or visibility
      // recovery. Native hide itself immediately revokes the capture epoch.
      if(!collectingRef.current){try{void callbacks.current.onSuspendCollection?.().catch(()=>{});}catch{}}
      try{await hide(true,true,collectingRef.current);if(live)setError('');}catch{}
    };
    const resize=async()=>{
      queued=true;if(resizing)return;resizing=true;
      try{while(queued&&live){queued=false;const owned=current.current;if(owned===null||!viewport.current||visibilityBusy.current||locked.current||externalBusyRef.current)break;
        if(hidden.current){
          if(!recover.current||!visibleRef.current||document.visibilityState==='hidden'||Date.now()<nextRecovery)break;
          try{visibleBounds(viewport.current);}catch{break;}
          nextRecovery=Date.now()+1000;await show(false);lastBounds=null;continue;
        }
        try{const next=visibleBounds(viewport.current);if(lastBounds&&next.x===lastBounds.x&&next.y===lastBounds.y&&next.width===lastBounds.width&&next.height===lastBounds.height)continue;await client.resize(owned,next);if(live&&!hidden.current&&current.current===owned)lastBounds=next;}catch{await fail(owned);break;}
      }}finally{resizing=false;}
    };
    const check=async()=>{const owned=current.current;if(owned===null||checking||visibilityBusy.current)return;if(hidden.current){void resize();return;}checking=true;try{const result=await client.status();if(live&&!hidden.current&&current.current===owned){statusFailures=0;setError(message=>message===statusNotice?'':message);if(!result.open||result.generation!==owned){recover.current=false;current.current=null;callbacks.current.onGeneration?.(null);setOpened(false);setShown(false);}}}catch{if(live&&!hidden.current&&current.current===owned&&++statusFailures>=2)setError(statusNotice);}finally{checking=false;}};
    const observer=typeof ResizeObserver==='undefined'?null:new ResizeObserver(()=>void resize());
    if(viewport.current)observer?.observe(viewport.current);
    const timer=setInterval(()=>void check(),1000);
    const update=()=>void resize();const wake=()=>{if(document.visibilityState!=='hidden'){nextRecovery=0;void resize();}};
    window.addEventListener('resize',update);window.addEventListener('scroll',update,true);window.addEventListener('focus',wake);document.addEventListener('visibilitychange',wake);
    const track=()=>{if(!live)return;if(!hidden.current)void resize();frame=requestAnimationFrame(track);};frame=requestAnimationFrame(track);
    return()=>{live=false;cancelAnimationFrame(frame);observer?.disconnect();clearInterval(timer);window.removeEventListener('resize',update);window.removeEventListener('scroll',update,true);window.removeEventListener('focus',wake);document.removeEventListener('visibilitychange',wake);};
  },[client,opened,visible,hide,show]);
  return <section className="panel threads-browser" aria-labelledby="threads-browser-heading">
    <div className="section-heading"><h2 id="threads-browser-heading">Threads browser</h2><div className="actions"><button className="primary" disabled={externalBusy||pending||opened} onClick={()=>void open()}>{pending&&!opened?'Opening…':'Open Threads'}</button>{opened&&!shown&&visible&&<button disabled={externalBusy||pending} onClick={()=>void show()}>Show Threads</button>}<button disabled={externalBusy||pending||!opened} onClick={()=>void close()}>Close Threads</button></div></div>
    <p role="status">{opened?(shown?'Threads is open in this window.':backgrounded&&collecting?'Threads is hidden. Collection continues in the background.':'Threads remains open and is hidden.'):'Threads is closed. Opening it does not start a collection session.'}</p>
    <p className="muted">Browse public posts without logging in, or sign in here. Changing pages keeps this browser and your session open.</p>
    {error&&<p role="alert" className="error">{error}</p>}
    <div ref={viewport} className="threads-viewport" aria-label="Embedded Threads viewport">{!opened&&<p>Open Threads to browse public posts here.</p>}</div>
  </section>;
}
