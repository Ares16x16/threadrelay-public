import { act, renderHook } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import type { ActivityPage } from '../../lib/contracts';
import { useActivity } from './useActivity';

import { entry, page } from './testFixtures';
afterEach(()=>vi.useRealTimers());

it('drains forward pages, accepts sequence gaps and deduplicates in chronological order',async()=>{
 vi.useFakeTimers();
 const worker={queryActivity:vi.fn().mockResolvedValueOnce(page([entry('1'),entry('3')])).mockResolvedValueOnce(page([entry('5')],{has_more:true})).mockResolvedValueOnce(page([entry('5'),entry('9')])).mockResolvedValue(page([],{cursor:'9'}))};
 const{result}=renderHook(()=>useActivity(worker));await act(async()=>{});
 await act(async()=>vi.advanceTimersByTimeAsync(1000));await act(async()=>vi.advanceTimersByTimeAsync(1));
 expect(result.current.entries.map(item=>item.sequence)).toEqual(['1','3','5','9']);
 expect(worker.queryActivity).toHaveBeenNthCalledWith(2,{after:'3',limit:50});
 expect(worker.queryActivity).toHaveBeenNthCalledWith(3,{after:'5',limit:50});
});
it('keeps failed observations stale and retries repeated failures with read queries only',async()=>{
 vi.useFakeTimers();const worker={queryActivity:vi.fn().mockResolvedValueOnce(page([entry('1')])).mockRejectedValueOnce(new Error('offline')).mockRejectedValueOnce(new Error('offline')).mockResolvedValue(page([entry('2')]))};
 const{result}=renderHook(()=>useActivity(worker));await act(async()=>{});
 await act(async()=>vi.advanceTimersByTimeAsync(1000));expect(result.current.error).not.toBe('');expect(result.current.entries).toHaveLength(1);
 await act(async()=>vi.advanceTimersByTimeAsync(2000));expect(result.current.error).toBe('');expect(result.current.entries).toHaveLength(2);
});
it('rejects a forward page which claims more without advancing its cursor',async()=>{
 vi.useFakeTimers();const worker={queryActivity:vi.fn().mockResolvedValueOnce(page([entry('1')])).mockResolvedValue(page([],{cursor:'1',has_more:true}))};
 const{result}=renderHook(()=>useActivity(worker));await act(async()=>{});await act(async()=>vi.advanceTimersByTimeAsync(1000));
 expect(result.current.error).not.toBe('');expect(result.current.entries.map(item=>item.sequence)).toEqual(['1']);
});
it('freezes display without collection actions and discards late or unmounted reads',async()=>{
 vi.useFakeTimers();let finish!:(value:ActivityPage)=>void;
 const worker={queryActivity:vi.fn().mockResolvedValueOnce(page([entry('1')])).mockImplementationOnce(()=>new Promise< ActivityPage>(resolve=>{finish=resolve;})).mockResolvedValue(page([entry('3')]))};
 const{result,unmount}=renderHook(()=>useActivity(worker));await act(async()=>{});await act(async()=>vi.advanceTimersByTimeAsync(1000));
 act(()=>result.current.freeze());await act(async()=>finish(page([entry('2')])));expect(result.current.entries.map(item=>item.sequence)).toEqual(['1']);
 await act(async()=>vi.advanceTimersByTimeAsync(3000));expect(worker.queryActivity).toHaveBeenCalledTimes(2);
 act(()=>result.current.follow());await act(async()=>{});expect(worker.queryActivity).toHaveBeenLastCalledWith({after:'1',limit:50});
 expect(result.current.entries.map(item=>item.sequence)).toEqual(['1','3']);
 unmount();await act(async()=>vi.advanceTimersByTimeAsync(5000));expect(worker.queryActivity).toHaveBeenCalledTimes(3);
});
it('never overlaps a slow read and stops reads while the retained monitor is hidden',async()=>{
 vi.useFakeTimers();let finish!:(value:ActivityPage)=>void;const worker={queryActivity:vi.fn(()=>new Promise<ActivityPage>(resolve=>{finish=resolve;}))};
 const{result,rerender}=renderHook(({enabled})=>useActivity(worker,enabled),{initialProps:{enabled:true}});
 await act(async()=>vi.advanceTimersByTimeAsync(5000));expect(worker.queryActivity).toHaveBeenCalledTimes(1);
 rerender({enabled:false});await act(async()=>finish(page([entry('1')])));expect(result.current.entries).toEqual([]);
 await act(async()=>vi.advanceTimersByTimeAsync(3000));expect(worker.queryActivity).toHaveBeenCalledTimes(1);
});
it('bounds the window and loads older records without moving the live cursor backwards',async()=>{
 vi.useFakeTimers();const initial=Array.from({length:300},(_,i)=>entry(String(i+101)));
 const worker={queryActivity:vi.fn().mockResolvedValueOnce(page(initial,{older_cursor:'101'})).mockResolvedValueOnce(page([entry('99'),entry('100')],{cursor:'100',older_cursor:'99'})).mockResolvedValue(page([entry('401')],{older_cursor:'401'}))};
 const{result}=renderHook(()=>useActivity(worker));await act(async()=>{});act(()=>result.current.loadOlder());await act(async()=>{});
 expect(worker.queryActivity).toHaveBeenLastCalledWith({before:'101',limit:50});expect(result.current.following).toBe(false);expect(result.current.entries).toHaveLength(300);expect(result.current.entries[0].sequence).toBe('99');
 act(()=>result.current.follow());await act(async()=>{});expect(worker.queryActivity).toHaveBeenLastCalledWith({limit:50});expect(result.current.entries.map(item=>item.sequence)).toEqual(['401']);
});
