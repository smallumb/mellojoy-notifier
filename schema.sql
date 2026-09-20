-- 監視状態（前回の商品スナップショット・失敗回数など）を1行のJSONで保持する
CREATE TABLE IF NOT EXISTS state (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
