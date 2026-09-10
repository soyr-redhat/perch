const storedTheme = localStorage.getItem('perch.theme') || 'system';
document.documentElement.dataset.theme = storedTheme === 'system' ? (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light') : storedTheme;
