.PHONY: build build-publisher build-previewer test

# Existing publisher-only build entry point.
build: build-publisher

build-publisher:
	docker build -t zenn-authoring-publisher publisher

build-previewer:
	docker build -t zenn-authoring-previewer previewer

test:
	docker run --rm --entrypoint python --mount "type=bind,source=$(CURDIR)/publisher,target=/src,readonly" -w /src zenn-authoring-publisher -m unittest discover -s tests -v
