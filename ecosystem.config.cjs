module.exports = {
  apps: [
    {
      name: 'medialab',
      cwd: __dirname,
      script: 'server.py',
      interpreter: 'python3',
      args: ['4173'],
      exec_mode: 'fork',
      instances: 1,
      watch: false,
      autorestart: true,
      restart_delay: 1000,
      env: {
        PYTHONUNBUFFERED: '1',
      },
    },
  ],
};
