import { useCallback, useEffect, useRef, useState } from 'react';

/** Validated read-only loader. Superseded/unmounted replies are never applied.
 * Observation schedules at most one next read after success. Errors stop it
 * until an explicit safe refresh; neither path replays a mutation or connects. */
export function useResource<T>(load:()=>Promise<T>, observeMs?:number) {
  const [value,setValue] = useState<T | null>(null), [error,setError] = useState(''), [loading,setLoading] = useState(false), [revision,setRevision] = useState(0);
  const generation=useRef(0), active=useRef(false), inFlight=useRef(false), hasValue=useRef(false), timer=useRef<ReturnType<typeof setTimeout>|undefined>(undefined);
  const refreshRef=useRef<()=>Promise<T|null>>(async()=>null);
  const refresh = useCallback(async (options?:{background?:boolean}):Promise<T|null> => {
    if(!active.current)return null;
    if(options?.background && inFlight.current)return null;
    if(timer.current){clearTimeout(timer.current);timer.current=undefined;}
    const current=++generation.current; inFlight.current=true;
    // A read-only status observation keeps the current expiring QR visible.
    // Foreground reads still revoke editing authority while waiting. A failed
    // background read sets the same error and never replays a mutation.
    if(!options?.background || !hasValue.current){setLoading(true);setError('');}
    let succeeded=false;
    try {
      const result=await load();
      if(generation.current!==current)return null;
      hasValue.current=true;setValue(result);setError('');setRevision(n=>n+1);succeeded=true;return result;
    } catch(e) {
      if(generation.current===current)setError(e instanceof Error && e.message==='Unavailable in this build.' ? e.message : 'Could not load current data. Retry when the worker is available.');
      return null;
    } finally {
      if(generation.current===current && active.current){
        inFlight.current=false;setLoading(false);
        if(succeeded && observeMs!==undefined)timer.current=setTimeout(()=>void refreshRef.current(),observeMs);
      }
    }
  },[load,observeMs]);
  refreshRef.current=refresh;
  useEffect(()=>{active.current=true;void refresh();return()=>{active.current=false;generation.current++;if(timer.current)clearTimeout(timer.current);timer.current=undefined;};},[refresh]);
  return {value,error,loading,refresh,revision};
}
