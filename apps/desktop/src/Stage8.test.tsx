import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import App from './App';

describe('operational shell', () => {
  it('provides labeled views including embedded Threads without starting a session', () => {
    render(<App />);
    for (const name of ['Dashboard', 'Threads', 'Review inbox', 'Topics', 'Routes', 'History', 'Accounts', 'Settings']) {
      fireEvent.click(screen.getByRole('button', { name }));
      expect(screen.getByRole('heading', { level: 1, name })).toBeVisible();
    }
  });
  it('explains dedicated profile and does not claim normal browser transfer', () => {
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: 'Accounts' }));
    const profile=screen.getByText('Threads sign-in and privacy');
    expect(profile.closest('details')).not.toHaveAttribute('open');
    fireEvent.click(profile);
    expect(screen.getByText(/session is separate from your everyday browser/i)).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Go to Threads' }));
    expect(screen.getByRole('heading',{level:1,name:'Threads'})).toBeVisible();
    expect(screen.getByRole('button',{name:'Open Threads'})).toBeVisible();
  });
});
