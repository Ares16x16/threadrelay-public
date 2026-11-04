import { fireEvent, render, screen } from '@testing-library/react';
import axe from 'axe-core';
import { expect, it, vi } from 'vitest';
vi.mock('@tauri-apps/api/core',()=>({invoke:vi.fn().mockRejectedValue(new Error('offline'))}));
import App from './App';

it('has no automatically detected structural accessibility violations in each unavailable-state view', async () => {
  const {container}=render(<App/>);
  for (const view of ['Dashboard','Review inbox','Topics','Routes','History','Accounts','Settings']) {
    fireEvent.click(screen.getByRole('button',{name:view}));
    const result=await axe.run(container,{rules:{'color-contrast':{enabled:false}}});
    expect(result.violations.map(v=>`${view}: ${v.id} ${v.help}`)).toEqual([]);
  }
});
