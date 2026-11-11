-- Revenue per country, from the cleaned orders.
SELECT country, count(*) AS orders, sum(amount) AS revenue
FROM clean_orders
GROUP BY country
ORDER BY revenue DESC
