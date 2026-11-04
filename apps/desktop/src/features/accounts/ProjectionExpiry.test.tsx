import { execFileSync } from 'node:child_process';
import { resolve } from 'node:path';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { validateContract, type AccountView } from '../../lib/contracts';
import { TelegramPanel } from './View';

it('clears the input at the expiry projected by the real AuthController adapter',()=>{
  const python=resolve(process.cwd(),'../../worker/.venv',process.platform==='win32'?'Scripts/python.exe':'bin/python');
  const fixture=JSON.parse(execFileSync(python,[resolve(process.cwd(),'../../worker/tests/auth_projection_fixture.py')],{encoding:'utf8'})) as AccountView;
  expect(validateContract('AccountView',fixture)).toBe(true);
  vi.useFakeTimers();vi.setSystemTime(new Date('2030-01-01T00:00:00Z'));
  const submit=vi.fn(),begin=vi.fn();
  try {
    render(<TelegramPanel view={fixture} secure={{submit}} cancel={async()=>{}} begin={begin} logout={async()=>{}} refresh={()=>{}} authoritative/>);
    const input=screen.getByLabelText('Login code') as HTMLInputElement;
    fireEvent.change(input,{target:{value:'synthetic'}});
    act(()=>vi.advanceTimersByTime(5000));
    expect(screen.queryByLabelText('Login code')).not.toBeInTheDocument();
    expect(input.value).toBe(''); expect(submit).not.toHaveBeenCalled();expect(begin).not.toHaveBeenCalled();
  } finally {vi.useRealTimers();}
});
