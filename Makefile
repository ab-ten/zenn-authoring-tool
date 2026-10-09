
build:
	docker build -t zenn-authoring-publisher publisher

test:
	docker run --rm --entrypoint python --mount "type=bind,source=$(CURDIR)/publisher,target=/src,readonly" -w /src zenn-authoring-publisher -m unittest discover -s tests -v
