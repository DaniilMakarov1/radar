SELECT
    table_schema,
    table_name,
    column_name,
    data_type
FROM information_schema.columns
WHERE table_schema IN ('dex_solana', 'dex')
  AND table_name IN ('trades')
ORDER BY table_schema, table_name, ordinal_position;
