from bootstrap import load_create_app

create_app = load_create_app()
app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=app.config.get("DEBUG", False))
