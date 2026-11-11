-- Paid orders with their customer's country.
SELECT o.id, o.amount, c.country
FROM '../orders.csv' o
JOIN '../customers.csv' c ON c.id = o.customer_id
WHERE o.status = 'paid'
