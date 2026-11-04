import { useEffect, useRef, useState } from 'react';
import type { ActivityEntry, ActivityPage, ActivityQuery } from '../../lib/contracts';
import type { WorkerClient } from '../../lib/useWorker';

export type ActivityWorker=Pick<WorkerClient,'queryActivity'>;
const LIMIT=50,MAX_ITEMS=300;
type Direction='latest'|'after'|'older';
function ordered(entries:ActivityEntry[]) {
 return [...new Map(entries.map(entry=>[entry.sequence,entry])).values()].sort((a,b)=>BigInt(a.sequence)<BigInt(b.sequence)?-1:BigInt(a.sequence)>BigInt(b.sequence)?1:0);
}
/** Observation only: cursors refer to committed journal rows, never commands. */
export function useActivity(worker:ActivityWorker,enabled=true) {
 const [entries,setEntries]=useState<ActivityEntry[]>([]),[snapshot,setSnapshot]=useState<ActivityPage|null>(null);
 const [following,setFollowing]=useState(true),[loading,setLoading]=useState(false),[error,setError]=useState(''),[revision,setRevision]=useState(0),[catchingUp,setCatchingUp]=useState(false);
 const rows=useRef<ActivityEntry[]>([]),cursor=useRef<string|null>(null),older=useRef<string|null>(null),inFlight=useRef(false),generation=useRef(0),manual=useRef<Direction|null>(null),browsingOlder=useRef(false);
 const [olderCursor,setOlderCursor]=useState<string|null>(null);
 useEffect(()=>{
  if(!enabled||(!following&&manual.current===null)){setLoading(false);return;}
  const current=++generation.current;let live=true,timer:ReturnType<typeof setTimeout>|undefined,batches=0;
  let direction:Direction=manual.current??(cursor.current===null?'latest':'after');manual.current=null;
  const read=async()=>{
   if(!live)return;
   if(inFlight.current){timer=setTimeout(()=>void read(),1000);return;}
   const requested=direction;
   const from=requested==='older'?older.current:requested==='after'?cursor.current:null;
   if(requested==='older'&&from===null)return;
   const payload:ActivityQuery={limit:LIMIT,...(from===null?{}:requested==='older'?{before:from}:{after:from})};
   inFlight.current=true;setLoading(true);let more=false;
   try {
    const value=await worker.queryActivity(payload);
    if(!live||generation.current!==current)return;
    if(requested==='after'&&(BigInt(value.cursor)<BigInt(from!)||(value.has_more&&BigInt(value.cursor)<=BigInt(from!))))throw new Error('Activity cursor did not advance');
    let next=ordered(requested==='latest'?value.entries:[...rows.current,...value.entries]);
    const trimmed=next.length>MAX_ITEMS;
    next=requested==='older'?next.slice(0,MAX_ITEMS):next.slice(-MAX_ITEMS);
    rows.current=next;setEntries(next);setSnapshot(value);setError('');
    if(requested==='older')older.current=value.older_cursor;
    else {
     cursor.current=value.cursor;
     if(requested==='latest')older.current=value.older_cursor;
     if(trimmed)older.current=next[0]?.sequence??null;
     more=value.has_more;
    }
    setOlderCursor(older.current);setCatchingUp(more);direction='after';
   } catch {
    if(live&&generation.current===current){setError('Activity could not be refreshed. Displayed events are from the last successful read.');setCatchingUp(false);}
   } finally {
    inFlight.current=false;
    if(live&&generation.current===current){
     setLoading(false);
     if(following){batches=more?batches+1:0;timer=setTimeout(()=>void read(),more&&batches<6?0:1000);if(batches>=6)batches=0;}
    }
   }
  };
  void read();
  return()=>{live=false;generation.current++;if(timer!==undefined)clearTimeout(timer);};
 },[worker,enabled,following,revision]);
 const freeze=()=>setFollowing(false);
 const follow=()=>{
  if(browsingOlder.current){cursor.current=null;older.current=null;rows.current=[];setEntries([]);setOlderCursor(null);browsingOlder.current=false;manual.current='latest';}
  setFollowing(true);setRevision(value=>value+1);
 };
 const retry=()=>{manual.current=browsingOlder.current?'older':cursor.current===null?'latest':'after';setRevision(value=>value+1);};
 const loadOlder=()=>{
  if(inFlight.current||older.current===null)return;
  browsingOlder.current=true;manual.current='older';setFollowing(false);setRevision(value=>value+1);
 };
 return {entries,snapshot,following,loading,error,catchingUp,olderCursor,freeze,follow,retry,loadOlder};
}
