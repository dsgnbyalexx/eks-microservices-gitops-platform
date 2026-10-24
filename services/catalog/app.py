"""Catalog service: read-only product list. Reference data ships with the image,
so the service is stateless and any replica can answer any request."""

from flask import abort, jsonify

from common.app_factory import create_app

PRODUCTS = {
    "mug": {"id": "mug", "name": "Cloud Mug", "price_cents": 1499, "currency": "USD"},
    "tee": {"id": "tee", "name": "YAML Engineer Tee", "price_cents": 2499, "currency": "USD"},
    "stickers": {
        "id": "stickers",
        "name": "Kubernetes Sticker Pack",
        "price_cents": 599,
        "currency": "USD",
    },
    "hoodie": {"id": "hoodie", "name": "On-Call Hoodie", "price_cents": 4999, "currency": "USD"},
}


def create():
    app = create_app("catalog")

    @app.get("/catalog/products")
    def list_products():
        return jsonify(products=list(PRODUCTS.values()))

    @app.get("/catalog/products/<product_id>")
    def get_product(product_id: str):
        product = PRODUCTS.get(product_id)
        if product is None:
            abort(404, description="product not found")
        return jsonify(product=product)

    return app
