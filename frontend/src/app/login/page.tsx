'use client';
import { FormEvent, useState } from 'react';
import { useRouter } from 'next/navigation';
import Link from 'next/link';
import { authApi, errorMessage } from '@/lib/api';

export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [email, setEmail] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError('');
    setBusy(true);
    try {
      if (mode === 'register') {
        await authApi.register({ email, username, password });
      }
      await authApi.login(email, password);
      router.push('/dashboard');
    } catch (err) {
      setError(errorMessage(err, mode === 'login' ? 'Login failed' : 'Registration failed'));
    }
    setBusy(false);
  };

  const input = 'w-full rounded-xl px-3 py-2 text-sm outline-none';
  const inputStyle = { background: '#1a1d24', border: '1px solid #1e2535', color: '#e8eaf0' };

  return (
    <div className="min-h-screen flex items-center justify-center p-4" style={{ background: '#0a0b0d' }}>
      <form onSubmit={submit} className="card w-full max-w-sm p-6 space-y-4">
        <div>
          <h1 className="text-xl font-bold" style={{ color: '#e8eaf0' }}>
            AI<span style={{ color: '#4fa3ff' }}>Trade</span>
          </h1>
          <p className="text-xs mt-1" style={{ color: '#5a6478' }}>
            {mode === 'login' ? 'Sign in to your account' : 'Create an account'}
          </p>
        </div>
        <label className="block space-y-1">
          <span className="text-xs" style={{ color: '#9ba3b8' }}>Email</span>
          <input className={input} style={inputStyle} type="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </label>
        {mode === 'register' && (
          <label className="block space-y-1">
            <span className="text-xs" style={{ color: '#9ba3b8' }}>Username (letters and numbers)</span>
            <input className={input} style={inputStyle} required minLength={3} pattern="[A-Za-z0-9]+" value={username}
              onChange={(e) => setUsername(e.target.value)} />
          </label>
        )}
        <label className="block space-y-1">
          <span className="text-xs" style={{ color: '#9ba3b8' }}>Password</span>
          <input className={input} style={inputStyle} type="password" required minLength={8} value={password}
            onChange={(e) => setPassword(e.target.value)} />
        </label>
        {error && <p className="text-xs" style={{ color: '#ff4757' }}>{error}</p>}
        <button type="submit" disabled={busy} className="w-full py-2 rounded-xl text-sm font-semibold"
          style={{ background: '#4fa3ff', color: 'white', opacity: busy ? 0.6 : 1 }}>
          {busy ? 'Please wait…' : mode === 'login' ? 'Sign in' : 'Create account'}
        </button>
        <div className="flex justify-between text-xs">
          <button type="button" onClick={() => setMode(mode === 'login' ? 'register' : 'login')} style={{ color: '#4fa3ff' }}>
            {mode === 'login' ? 'Create an account' : 'I already have an account'}
          </button>
          <Link href="/dashboard" style={{ color: '#5a6478' }}>Back to dashboard</Link>
        </div>
      </form>
    </div>
  );
}
