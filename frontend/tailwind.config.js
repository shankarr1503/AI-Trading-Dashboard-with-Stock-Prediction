/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './src/pages/**/*.{js,ts,jsx,tsx,mdx}',
    './src/components/**/*.{js,ts,jsx,tsx,mdx}',
    './src/app/**/*.{js,ts,jsx,tsx,mdx}',
  ],
  theme: {
    extend: {
      colors: {
        // Trading dark theme
        bg: {
          primary: '#0a0b0d',
          secondary: '#111318',
          tertiary: '#1a1d24',
          elevated: '#1e2229',
          card: '#141720',
        },
        accent: {
          green: '#00d4aa',
          red: '#ff4757',
          blue: '#4fa3ff',
          yellow: '#ffd700',
          purple: '#a855f7',
          cyan: '#06b6d4',
        },
        text: {
          primary: '#e8eaf0',
          secondary: '#9ba3b8',
          muted: '#5a6478',
          success: '#00d4aa',
          error: '#ff4757',
          warning: '#ffd700',
        },
        border: {
          default: '#1e2535',
          subtle: '#151a23',
        },
      },
      fontFamily: {
        // Self-hosted by next/font (see app/layout.tsx): no runtime requests to Google.
        sans: ['var(--font-inter)', 'system-ui', 'sans-serif'],
        mono: ['var(--font-mono)', 'Fira Code', 'monospace'],
      },
      boxShadow: {
        'glow-green': '0 0 20px rgba(0, 212, 170, 0.15)',
        'glow-red': '0 0 20px rgba(255, 71, 87, 0.15)',
        'glow-blue': '0 0 20px rgba(79, 163, 255, 0.15)',
        card: '0 4px 24px rgba(0, 0, 0, 0.4)',
      },
      animation: {
        'pulse-green': 'pulse-green 2s infinite',
        'ticker-scroll': 'ticker-scroll 30s linear infinite',
        'fade-in': 'fadeIn 0.3s ease-in',
        'slide-up': 'slideUp 0.3s ease-out',
      },
      keyframes: {
        'pulse-green': {
          '0%, 100%': { opacity: 1 },
          '50%': { opacity: 0.5 },
        },
        'ticker-scroll': {
          '0%': { transform: 'translateX(0)' },
          '100%': { transform: 'translateX(-50%)' },
        },
        fadeIn: {
          '0%': { opacity: 0 },
          '100%': { opacity: 1 },
        },
        slideUp: {
          '0%': { transform: 'translateY(10px)', opacity: 0 },
          '100%': { transform: 'translateY(0)', opacity: 1 },
        },
      },
    },
  },
  plugins: [],
};
