import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { EmbeddedThreadsPane } from './EmbeddedThreadsPane';
import type { EmbeddedThreadsHandle } from './EmbeddedThreadsPane';

it('opens and reveals the browser only for the explicit collection preparation action',async()=>{
 vi.spyOn(HTMLElement.prototype,'getBoundingClientRect').mockReturnValue(bounds);
 const client=makeClient();const holder:{value:EmbeddedThreadsHandle|null}={value:null};
 render(<EmbeddedThreadsPane client={client} onHandle={value=>{holder.value=value;}}/>);
 expect(client.open).not.toHaveBeenCalled();
 expect(holder.value?.ensureOpen).toBeTypeOf('function');
 await act(async()=>holder.value!.ensureOpen!());
 expect(client.open).toHaveBeenCalledTimes(1);expect(client.show).toHaveBeenCalledWith(7,{x:240,y:220,width:600,height:400});
 await act(async()=>holder.value!.ensureOpen!());
 expect(client.open).toHaveBeenCalledTimes(1);
});
it('refuses collection preparation while the Threads page is hidden',async()=>{
 const client=makeClient();const holder:{value:EmbeddedThreadsHandle|null}={value:null};
 render(<EmbeddedThreadsPane visible={false} client={client} onHandle={value=>{holder.value=value;}}/>);
 await expect(holder.value!.ensureOpen!()).rejects.toThrow('Threads is not available');
 expect(client.open).not.toHaveBeenCalled();expect(client.show).not.toHaveBeenCalled();
});

const bounds={x:240,y:220,left:240,top:220,right:840,bottom:620,width:600,height:400,toJSON:()=>({})};
const makeClient=()=>({background:vi.fn().mockResolvedValue(undefined),hide:vi.fn().mockResolvedValue(undefined),show:vi.fn().mockResolvedValue(undefined),open:vi.fn().mockResolvedValue({open:true,generation:7}),resize:vi.fn().mockResolvedValue(undefined),close:vi.fn().mockResolvedValue(undefined),status:vi.fn().mockResolvedValue({open:true,generation:7})});

it('preserves the capture lease through deliberate background navigation and return',async()=>{
 vi.spyOn(HTMLElement.prototype,'getBoundingClientRect').mockReturnValue(bounds);
 const client=makeClient(),suspend=vi.fn();const holder:{value:EmbeddedThreadsHandle|null}={value:null};
 const props={client,collecting:true,onHandle:(value:EmbeddedThreadsHandle|null)=>{holder.value=value;},onSuspendCollection:suspend};
 const {rerender}=render(<EmbeddedThreadsPane {...props}/>);
 fireEvent.click(screen.getByRole('button',{name:'Open Threads'}));await screen.findByText('Threads is open in this window.');
 await act(async()=>holder.value!.hide({preserveCapture:true}));rerender(<EmbeddedThreadsPane {...props} visible={false}/>);
 expect(client.background).toHaveBeenCalledTimes(1);expect(client.hide).not.toHaveBeenCalled();expect(suspend).not.toHaveBeenCalled();
 rerender(<EmbeddedThreadsPane {...props}/>);await waitFor(()=>expect(client.show).toHaveBeenCalledWith(7,{x:240,y:220,width:600,height:400}));
 expect(client.open).toHaveBeenCalledTimes(1);expect(client.close).not.toHaveBeenCalled();
});

it('does not let the first opened effect show over an already requested navigation hide',async()=>{
 vi.spyOn(HTMLElement.prototype,'getBoundingClientRect').mockReturnValue(bounds);
 vi.spyOn(window,'requestAnimationFrame').mockReturnValue(1);vi.spyOn(window,'cancelAnimationFrame').mockImplementation(()=>{});
 const client=makeClient();const holder:{value:EmbeddedThreadsHandle|null}={value:null};let hidden:Promise<void>|undefined;
 const props={client,collecting:true,onHandle:(value:EmbeddedThreadsHandle|null)=>{holder.value=value;},onGeneration:(generation:number|null)=>{if(generation!==null)hidden=holder.value!.hide({preserveCapture:true});}};
 const {rerender}=render(<EmbeddedThreadsPane {...props}/>);
 fireEvent.click(screen.getByRole('button',{name:'Open Threads'}));
 await waitFor(()=>expect(client.background).toHaveBeenCalledTimes(1));await act(async()=>hidden);
 expect(client.show).not.toHaveBeenCalled();expect(screen.getByText('Threads is hidden. Collection continues in the background.')).toBeVisible();
 rerender(<EmbeddedThreadsPane {...props} visible={false}/>);
 expect(client.background).toHaveBeenCalledTimes(1);
 rerender(<EmbeddedThreadsPane {...props}/>);await waitFor(()=>expect(client.show).toHaveBeenCalledTimes(1));
});

it('uses background hiding for a temporarily unavailable collecting viewport without suspension',async()=>{
 let rect=bounds;vi.spyOn(HTMLElement.prototype,'getBoundingClientRect').mockImplementation(()=>rect);
 vi.spyOn(window,'requestAnimationFrame').mockReturnValue(1);vi.spyOn(window,'cancelAnimationFrame').mockImplementation(()=>{});
 const client=makeClient(),suspend=vi.fn();render(<EmbeddedThreadsPane collecting client={client} onSuspendCollection={suspend}/>);
 fireEvent.click(screen.getByRole('button',{name:'Open Threads'}));await screen.findByText('Threads is open in this window.');
 rect={...bounds,top:-500,bottom:-100};fireEvent.scroll(window);
 await waitFor(()=>expect(client.background).toHaveBeenCalledWith(7));expect(client.hide).not.toHaveBeenCalled();expect(suspend).not.toHaveBeenCalled();
});

it('does not navigate or embed remote content until the user opens Threads', () => {
  const client=makeClient();const {container}=render(<EmbeddedThreadsPane client={client}/>);
  expect(client.open).not.toHaveBeenCalled();
  expect(container.querySelector('iframe')).toBeNull();
  expect(screen.getByRole('button',{name:'Open Threads'})).toBeEnabled();
  expect(screen.getByText(/Browse public posts without logging in/)).toBeVisible();
});

it('opens inside the measured viewport and closes its exact generation', async () => {
  vi.spyOn(HTMLElement.prototype,'getBoundingClientRect').mockReturnValue(bounds);
  const client=makeClient();render(<EmbeddedThreadsPane client={client}/>);
  fireEvent.click(screen.getByRole('button',{name:'Open Threads'}));
  await screen.findByText('Threads is open in this window.');
  expect(client.open).toHaveBeenCalledWith({x:240,y:220,width:600,height:400});
  fireEvent.click(screen.getByRole('button',{name:'Close Threads'}));
  await waitFor(()=>expect(client.close).toHaveBeenCalledWith(7));
  expect(screen.getByRole('button',{name:'Open Threads'})).toBeEnabled();
});

it('closes a late native open after navigation away without reopening', async () => {
  vi.spyOn(HTMLElement.prototype,'getBoundingClientRect').mockReturnValue(bounds);
  let finish!:(value:{open:boolean;generation:number})=>void;
  const client=makeClient();client.open.mockImplementation(()=>new Promise(resolve=>{finish=resolve;}));
  const {unmount}=render(<EmbeddedThreadsPane client={client}/>);
  fireEvent.click(screen.getByRole('button',{name:'Open Threads'}));
  unmount();
  await act(async()=>finish({open:true,generation:9}));
  expect(client.close).toHaveBeenCalledWith(9);
  expect(client.open).toHaveBeenCalledTimes(1);
});

it('updates native bounds after a position-only layout change', async () => {
  let rect=bounds,frame:FrameRequestCallback|undefined;
  vi.spyOn(HTMLElement.prototype,'getBoundingClientRect').mockImplementation(()=>rect);
  vi.spyOn(window,'requestAnimationFrame').mockImplementation(callback=>{frame=callback;return 1;});
  vi.spyOn(window,'cancelAnimationFrame').mockImplementation(()=>{});
  const client=makeClient();render(<EmbeddedThreadsPane client={client}/>);
  fireEvent.click(screen.getByRole('button',{name:'Open Threads'}));
  await screen.findByText('Threads is open in this window.');
  expect(frame).toBeTypeOf('function');
  rect={...bounds,y:280,top:280,bottom:680};
  await act(async()=>frame!(16));
  expect(client.resize).toHaveBeenLastCalledWith(7,{x:240,y:280,width:600,height:400});
});

it('applies the latest geometry after an outstanding resize instead of dropping it', async () => {
  let rect=bounds,finish!:()=>void;
  vi.spyOn(HTMLElement.prototype,'getBoundingClientRect').mockImplementation(()=>rect);
  vi.spyOn(window,'requestAnimationFrame').mockReturnValue(1);
  vi.spyOn(window,'cancelAnimationFrame').mockImplementation(()=>{});
  const client=makeClient();client.resize.mockImplementationOnce(()=>new Promise<void>(resolve=>{finish=resolve;}));
  render(<EmbeddedThreadsPane client={client}/>);
  fireEvent.click(screen.getByRole('button',{name:'Open Threads'}));
  await screen.findByText('Threads is open in this window.');
  await waitFor(()=>expect(window.requestAnimationFrame).toHaveBeenCalled());
  fireEvent.resize(window);
  expect(client.resize).toHaveBeenCalledTimes(1);
  rect={...bounds,y:280,top:280,bottom:680};fireEvent.scroll(window);
  await act(async()=>finish());
  expect(client.resize).toHaveBeenCalledTimes(2);
  expect(client.resize).toHaveBeenLastCalledWith(7,{x:240,y:280,width:600,height:400});
});

it.each([
  {layout:'desktop status strip',left:184,top:44,right:960,bottom:700},
  {layout:'narrow navigation and status strip',left:0,top:240,right:480,bottom:700},
])('keeps the native child below the fixed $layout when main scrolls', async clip => {
  let rect={...bounds,left:clip.left+20,right:clip.right-20,top:clip.top+50,bottom:clip.top+400};
  vi.spyOn(HTMLElement.prototype,'getBoundingClientRect').mockImplementation(function(this:HTMLElement) {
    return this.tagName==='MAIN' ? {...bounds,...clip} : rect;
  });
  vi.spyOn(window,'requestAnimationFrame').mockReturnValue(1);
  vi.spyOn(window,'cancelAnimationFrame').mockImplementation(()=>{});
  const client=makeClient();const {container}=render(<main><EmbeddedThreadsPane client={client}/></main>);
  fireEvent.click(screen.getByRole('button',{name:'Open Threads'}));
  await screen.findByText('Threads is open in this window.');
  rect={...rect,top:clip.top-30,bottom:clip.top+350};
  fireEvent.scroll(container.querySelector('main')!);
  await waitFor(()=>expect(client.resize).toHaveBeenLastCalledWith(7,{
    x:clip.left+20,y:clip.top,width:clip.right-clip.left-40,height:350,
  }));
  rect={...rect,top:clip.top-400,bottom:clip.top-1};
  fireEvent.scroll(container.querySelector('main')!);
  await waitFor(()=>expect(client.hide).toHaveBeenCalledWith(7));
  expect(client.close).not.toHaveBeenCalled();
  expect(client.open).toHaveBeenCalledTimes(1);
});

it('hides and returns to the same browser without close or reload',async()=>{
 vi.spyOn(HTMLElement.prototype,'getBoundingClientRect').mockReturnValue(bounds);
 const client=makeClient();const {rerender}=render(<EmbeddedThreadsPane client={client} visible/>);
 fireEvent.click(screen.getByRole('button',{name:'Open Threads'}));await screen.findByText('Threads is open in this window.');
 rerender(<EmbeddedThreadsPane client={client} visible={false}/>);
 await waitFor(()=>expect(client.hide).toHaveBeenCalledWith(7));expect(client.close).not.toHaveBeenCalled();
 rerender(<EmbeddedThreadsPane client={client} visible/>);
 await waitFor(()=>expect(client.show).toHaveBeenCalledWith(7,{x:240,y:220,width:600,height:400}));
 expect(client.open).toHaveBeenCalledTimes(1);expect(client.close).not.toHaveBeenCalled();
});

it('ignores a late show after a newer hide without reviving geometry or closing the browser',async()=>{
 vi.spyOn(HTMLElement.prototype,'getBoundingClientRect').mockReturnValue(bounds);
 let finish!:()=>void;const client=makeClient();client.show.mockImplementationOnce(()=>new Promise<void>(resolve=>{finish=resolve;}));
 const {rerender}=render(<EmbeddedThreadsPane client={client} visible/>);
 fireEvent.click(screen.getByRole('button',{name:'Open Threads'}));await screen.findByText('Threads is open in this window.');
 rerender(<EmbeddedThreadsPane client={client} visible={false}/>);await waitFor(()=>expect(client.hide).toHaveBeenCalledTimes(1));
 rerender(<EmbeddedThreadsPane client={client} visible/>);await waitFor(()=>expect(client.show).toHaveBeenCalledTimes(1));
 rerender(<EmbeddedThreadsPane client={client} visible={false}/>);await waitFor(()=>expect(client.hide).toHaveBeenCalledTimes(2));
 await act(async()=>finish());expect(screen.getByText('Threads remains open and is hidden.')).toBeVisible();expect(client.close).not.toHaveBeenCalled();
});

it('does not close or reload after an old geometry request fails during deliberate hide',async()=>{
 vi.spyOn(HTMLElement.prototype,'getBoundingClientRect').mockReturnValue(bounds);
 vi.spyOn(window,'requestAnimationFrame').mockReturnValue(1);vi.spyOn(window,'cancelAnimationFrame').mockImplementation(()=>{});
 let reject!:(error:Error)=>void;const client=makeClient();client.resize.mockImplementationOnce(()=>new Promise<void>((_,fail)=>{reject=fail;}));
 const {rerender}=render(<EmbeddedThreadsPane client={client} visible/>);
 fireEvent.click(screen.getByRole('button',{name:'Open Threads'}));await screen.findByText('Threads is open in this window.');
 fireEvent.resize(window);expect(client.resize).toHaveBeenCalledTimes(1);
 rerender(<EmbeddedThreadsPane client={client} visible={false}/>);await waitFor(()=>expect(client.hide).toHaveBeenCalledWith(7));
 await act(async()=>reject(new Error('old native resize failed')));
 expect(client.close).not.toHaveBeenCalled();expect(client.open).toHaveBeenCalledTimes(1);expect(client.hide).toHaveBeenCalledTimes(1);
});

it('keeps a failed show hidden and retries visibility without another open',async()=>{
 vi.spyOn(HTMLElement.prototype,'getBoundingClientRect').mockReturnValue(bounds);
 const client=makeClient();client.show.mockRejectedValueOnce(new Error('unconfirmed'));
 const {rerender}=render(<EmbeddedThreadsPane client={client} visible/>);
 fireEvent.click(screen.getByRole('button',{name:'Open Threads'}));await screen.findByText('Threads is open in this window.');
 rerender(<EmbeddedThreadsPane client={client} visible={false}/>);await waitFor(()=>expect(client.hide).toHaveBeenCalledWith(7));
 rerender(<EmbeddedThreadsPane client={client} visible/>);await screen.findByText('Threads could not be shown. Retry Show Threads or close it.');
 fireEvent.click(screen.getByRole('button',{name:'Show Threads'}));await screen.findByText('Threads is open in this window.');
 expect(client.open).toHaveBeenCalledTimes(1);expect(client.close).not.toHaveBeenCalled();
});

it('brings an offscreen viewport into view before measuring a new browser',async()=>{
 let rect={...bounds,top:900,bottom:1300};
 vi.spyOn(HTMLElement.prototype,'getBoundingClientRect').mockImplementation(()=>rect);
 const old=Object.getOwnPropertyDescriptor(HTMLElement.prototype,'scrollIntoView');
 Object.defineProperty(HTMLElement.prototype,'scrollIntoView',{configurable:true,value:function(this:HTMLElement){if(this.getAttribute('aria-label')==='Embedded Threads viewport')rect=bounds;}});
 try {
  const client=makeClient();render(<EmbeddedThreadsPane client={client}/>);
  fireEvent.click(screen.getByRole('button',{name:'Open Threads'}));
  await screen.findByText('Threads is open in this window.');
  expect(client.open).toHaveBeenCalledWith({x:240,y:220,width:600,height:400});
 } finally {if(old)Object.defineProperty(HTMLElement.prototype,'scrollIntoView',old);else delete (HTMLElement.prototype as Partial<HTMLElement>).scrollIntoView;}
});

it('automatically restores the retained browser when its clipped viewport returns',async()=>{
 let rect=bounds;
 vi.spyOn(HTMLElement.prototype,'getBoundingClientRect').mockImplementation(()=>rect);
 vi.spyOn(window,'requestAnimationFrame').mockReturnValue(1);vi.spyOn(window,'cancelAnimationFrame').mockImplementation(()=>{});
 let finishSuspending!:()=>void;
 const suspend=vi.fn(()=>new Promise<void>(resolve=>{finishSuspending=resolve;}));
 const client=makeClient();render(<EmbeddedThreadsPane client={client} onSuspendCollection={suspend}/>);
 fireEvent.click(screen.getByRole('button',{name:'Open Threads'}));await screen.findByText('Threads is open in this window.');
 rect={...bounds,top:-420,bottom:-20};fireEvent.scroll(window);
 await waitFor(()=>expect(client.hide).toHaveBeenCalledWith(7));
 expect(suspend).toHaveBeenCalledTimes(1);
 rect=bounds;fireEvent.scroll(window);
 await waitFor(()=>expect(client.show).toHaveBeenCalledWith(7,{x:240,y:220,width:600,height:400}));
 await screen.findByText('Threads is open in this window.');
 expect(client.open).toHaveBeenCalledTimes(1);expect(client.close).not.toHaveBeenCalled();
 finishSuspending();
});

it('keeps the current browser after a transient read-only status failure',async()=>{
 vi.useFakeTimers({toFake:['setTimeout','clearTimeout','setInterval','clearInterval','Date']});
 try {
  vi.spyOn(HTMLElement.prototype,'getBoundingClientRect').mockReturnValue(bounds);
  vi.spyOn(window,'requestAnimationFrame').mockReturnValue(1);vi.spyOn(window,'cancelAnimationFrame').mockImplementation(()=>{});
  const client=makeClient();client.status.mockRejectedValueOnce(new Error('temporary native status delay'));
  render(<EmbeddedThreadsPane client={client}/>);
  await act(async()=>fireEvent.click(screen.getByRole('button',{name:'Open Threads'})));
  await act(async()=>vi.advanceTimersByTimeAsync(1000));
  expect(screen.getByText('Threads is open in this window.')).toBeVisible();
  expect(client.hide).not.toHaveBeenCalled();expect(client.close).not.toHaveBeenCalled();
  await act(async()=>vi.advanceTimersByTimeAsync(1000));
  expect(client.status).toHaveBeenCalledTimes(2);expect(client.open).toHaveBeenCalledTimes(1);
 } finally {vi.useRealTimers();}
});

it('retries a failed show on window focus without reopening or resuming collection',async()=>{
 vi.spyOn(HTMLElement.prototype,'getBoundingClientRect').mockReturnValue(bounds);
 vi.spyOn(window,'requestAnimationFrame').mockReturnValue(1);vi.spyOn(window,'cancelAnimationFrame').mockImplementation(()=>{});
 const client=makeClient();client.show.mockRejectedValueOnce(new Error('window was minimized'));
 const {rerender}=render(<EmbeddedThreadsPane client={client} visible/>);
 fireEvent.click(screen.getByRole('button',{name:'Open Threads'}));await screen.findByText('Threads is open in this window.');
 rerender(<EmbeddedThreadsPane client={client} visible={false}/>);await waitFor(()=>expect(client.hide).toHaveBeenCalledWith(7));
 fireEvent.focus(window);expect(client.show).not.toHaveBeenCalled();
 rerender(<EmbeddedThreadsPane client={client} visible/>);await screen.findByText('Threads could not be shown. Retry Show Threads or close it.');
 fireEvent.focus(window);
 await waitFor(()=>expect(client.show).toHaveBeenCalledTimes(2));
 await screen.findByText('Threads is open in this window.');
 expect(client.open).toHaveBeenCalledTimes(1);expect(client.close).not.toHaveBeenCalled();
});
