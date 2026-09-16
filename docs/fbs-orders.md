# FBS orders and supplies

The bot polls new FBS assembly orders every `ORDER_CHECK_INTERVAL` seconds (30 by default).

For each unseen order it shows Telegram inline buttons:

- a button for every compatible active supply;
- `Создать поставку и добавить` only when there is no compatible active supply;
- `Не добавлять`.

Compatibility uses the order/supply `cargoType` and `crossBorderType`. New orders are also filtered to the seller's single configured FBS warehouse.

When a new supply is created, the bot first adds the order and then creates exactly one cargo place (`trbx`). Adding later orders to an existing supply never creates an additional cargo place.

Before executing a Telegram action the bot re-checks that the order is still new and that the selected supply is still active and compatible. Final actions are persisted in SQLite to protect against duplicate clicks.

If the order was successfully assigned but WB rejects cargo-place creation, Telegram reports partial success instead of retrying the entire operation.
