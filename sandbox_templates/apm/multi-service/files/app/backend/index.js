const express = require('express');
const { Pool } = require('pg');

const app = express();
const pool = new Pool({
  host: process.env.PGHOST || 'db', user: 'shop', password: 'shop', database: 'shop',
});

const log = (level, msg) => console.log(JSON.stringify({ level, msg, service: 'inventory' }));

app.get('/items', async (req, res) => {
  log('info', 'listing items');
  const { rows } = await pool.query('SELECT id, name, stock FROM items ORDER BY id');
  res.json(rows);
});

app.get('/items/:id', async (req, res) => {
  const { rows } = await pool.query('SELECT id, name, stock FROM items WHERE id = $1', [req.params.id]);
  res.json(rows[0] || {});
});

app.listen(3000, () => log('info', 'inventory listening on 3000'));
