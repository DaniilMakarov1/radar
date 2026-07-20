SELECT
    blockchain,
    contract_address,
    symbol,
    name,
    decimals
FROM tokens.erc20
WHERE blockchain = 'base'
  AND symbol IN ('AIXBT', 'VIRTUAL', 'COOKIE')
ORDER BY symbol, contract_address
LIMIT 100;
