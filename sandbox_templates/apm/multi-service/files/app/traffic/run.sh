#!/bin/sh
# Steady, light traffic so there is always something to look at.
while true; do
  curl -s -o /dev/null http://storefront:8080/products
  curl -s -o /dev/null http://storefront:8080/checkout
  sleep 2
done
