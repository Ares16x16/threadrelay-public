import { fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { expect, it } from 'vitest';
import { ConfirmDialog } from './ConfirmDialog';
function Harness(){const [open,setOpen]=useState(false);return <><button onClick={()=>setOpen(true)}>Open</button>{open&&<ConfirmDialog title="Confirm action" confirmLabel="Proceed" onCancel={()=>setOpen(false)} onConfirm={()=>setOpen(false)}><p>This action changes identity.</p></ConfirmDialog>}</>}
it('starts on safe cancel, traps Tab, and returns focus on Escape',()=>{
 render(<Harness/>);const trigger=screen.getByRole('button',{name:'Open'});trigger.focus();fireEvent.click(trigger);
 const dialog=screen.getByRole('dialog',{name:'Confirm action'});expect(dialog).toHaveAttribute('aria-describedby');
 const cancel=screen.getByRole('button',{name:'Cancel'}),proceed=screen.getByRole('button',{name:'Proceed'});expect(cancel).toHaveFocus();
 fireEvent.keyDown(dialog,{key:'Tab',shiftKey:true});expect(proceed).toHaveFocus();
 fireEvent.keyDown(dialog,{key:'Escape'});expect(trigger).toHaveFocus();
});
