import { expect, it, vi } from 'vitest';
import { EmbeddedThreadsClient } from './embeddedThreads';

it('opens only the fixed native Threads surface with bounded geometry, never a URL', async () => {
  const invoke=vi.fn().mockResolvedValue({open:true,generation:7});
  const client=new EmbeddedThreadsClient(invoke);
  expect(await client.open({x:216,y:200,width:640,height:400})).toEqual({open:true,generation:7});
  expect(invoke).toHaveBeenCalledWith('embedded_threads_open',{bounds:{x:216,y:200,width:640,height:400}});
  expect(JSON.stringify(invoke.mock.calls)).not.toContain('https:');
});

it.each([{open:true,generation:0},{open:true,generation:NaN},{open:true,generation:1,extra:'untrusted'}])('rejects malformed native view status', async value => {
  const client=new EmbeddedThreadsClient(vi.fn().mockResolvedValue(value));
  await expect(client.status()).rejects.toThrow('Embedded Threads is unavailable.');
});

it('keeps generation on resize and close and refuses invalid bounds before invoke', async () => {
  const invoke=vi.fn().mockResolvedValue(undefined);
  const client=new EmbeddedThreadsClient(invoke);
  await client.resize(7,{x:216,y:200,width:640,height:400});
  await client.close(7);
  expect(invoke).toHaveBeenNthCalledWith(1,'embedded_threads_resize',{generation:7,revision:0,bounds:{x:216,y:200,width:640,height:400}});
  expect(invoke).toHaveBeenNthCalledWith(2,'embedded_threads_close',{generation:7});
  await expect(client.open({x:0,y:0,width:Infinity,height:1})).rejects.toThrow('Embedded Threads is unavailable.');
  expect(invoke).toHaveBeenCalledTimes(2);
});

it('orders exact-generation hide/show and rejects a late show acknowledgment after hide', async()=>{
 const rect={x:216,y:200,width:640,height:400};let finish!:()=>void;
 const invoke=vi.fn().mockImplementation(async(name:string)=>{if(name==='embedded_threads_show')await new Promise<void>(resolve=>{finish=resolve;});});
 const client=new EmbeddedThreadsClient(invoke);
 const showing=client.show(7,rect);const late=expect(showing).rejects.toThrow('Embedded Threads is unavailable.');
 await client.hide(7);finish();await late;
 expect(invoke).toHaveBeenNthCalledWith(1,'embedded_threads_show',{generation:7,revision:1,bounds:rect});
 expect(invoke).toHaveBeenNthCalledWith(2,'embedded_threads_hide',{generation:7,revision:2});
});

it('rejects a resize acknowledged after a newer hide intent',async()=>{
 let finish!:()=>void;const invoke=vi.fn().mockImplementation(async(name:string)=>{if(name==='embedded_threads_resize')await new Promise<void>(resolve=>{finish=resolve;});});
 const client=new EmbeddedThreadsClient(invoke);const resizing=client.resize(7,{x:0,y:0,width:10,height:10});const stale=expect(resizing).rejects.toThrow('Embedded Threads is unavailable.');
 await client.hide(7);finish();await stale;
});

it('orders background hiding with show and ordinary hide without accepting stale acknowledgments',async()=>{
 let finish!:()=>void;const invoke=vi.fn().mockImplementation(async(name:string)=>{if(name==='embedded_threads_background')await new Promise<void>(resolve=>{finish=resolve;});});
 const client=new EmbeddedThreadsClient(invoke);
 const background=client.background(7);const stale=expect(background).rejects.toThrow('Embedded Threads is unavailable.');
 await client.hide(7);finish();await stale;
 expect(invoke).toHaveBeenNthCalledWith(1,'embedded_threads_background',{generation:7,revision:1});
 expect(invoke).toHaveBeenNthCalledWith(2,'embedded_threads_hide',{generation:7,revision:2});
 await expect(client.background(0)).rejects.toThrow();expect(invoke).toHaveBeenCalledTimes(2);
});
