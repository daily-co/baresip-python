# baresip-python development targets. Everything runs inside the uv-managed venv.
#
#   native  - build the static libre/libbaresip libraries
#   ext     - compile the cffi extension in place (fast C iteration)
#   test    - run the unit test suite
#   check   - audit built artifacts' linkage (advisory here; CI enforces on release)
#   format  - apply formatting to all Python and C sources
#
# Sanitizers (memory errors in the C sources need instrumented builds to
# surface; CI runs these lanes on Linux):
#   ext-san   - rebuild the extension with ASan+UBSan. Overwrites the
#               in-place extension: run `make ext` afterwards to restore
#               the normal build (and keep `make check` meaningful).
#   test-san  - run the unit suite under the sanitized extension
#   ext-tsan  - rebuild the extension with ThreadSanitizer (TSan and
#               ASan are mutually exclusive — separate target pair,
#               separate build dir). Same restore caveat as ext-san.
#   test-tsan - run the thread-boundary test scope under TSan
#
# Bench (local FreeSWITCH in docker, see bench/README.md):
#   bench-up / bench-logs / bench-down

.PHONY: native ext ext-san ext-tsan test test-san test-tsan check format bench-up bench-logs bench-down

format:
	uv run ruff format src scripts tests
	uv run clang-format -i src/_native/*.c src/_native/*.h

native:
	uv run python scripts/build_native.py --prefix .native

ext:
	uv run python src/_native/build_ffi.py

test:
	uv run pytest tests/unit

ext-san:
	BP_SANITIZE=address,undefined uv run python src/_native/build_ffi.py

# What test-san runs; the nightly torture lane overrides this to point
# the same sanitized environment at tests/torture.
TEST_SAN_ARGS ?= tests/unit

ext-tsan:
	BP_SANITIZE=thread uv run python src/_native/build_ffi.py

# The TSan scope is deliberately narrow: the lane watches the
# thread-boundary architecture (re thread / pacing threads / log drain /
# Python threads over the rings and the handle table), not the whole
# suite. Expand it only if it catches something.
TSAN_TESTS = tests/unit/test_ring.py tests/unit/test_lifecycle.py \
	tests/unit/test_shutdown_paths.py tests/unit/test_runtime.py \
	tests/unit/test_audio.py

# The ASan runtime must own malloc from CPython's first allocation, so it
# is preloaded into the python binary itself — never via `uv run`: the
# loader applies the insertion to the first binary it starts (uv), and on
# macOS dyld also consumes the variable, so python would run
# uninstrumented. Reports go to a file, not stderr: pytest's fd capture
# would swallow the report of a crashing test, leaving only a bare
# faulthandler traceback; the cat replays it on failure.
ifeq ($(shell uname -s),Darwin)
# LeakSanitizer is unsupported on Apple Silicon: ASan+UBSan only here,
# leak detection is the Linux lane's job. verify_interceptors=0 is for
# the subprocess tests: dyld consumes the insertion variable, so their
# python loads ASan late — tolerated (heap checking is inert there)
# rather than aborting the whole run.
test-san:
	rm -f build/san-report.*
	DYLD_INSERT_LIBRARIES=$$(clang -print-file-name=libclang_rt.asan_osx_dynamic.dylib) \
	MallocNanoZone=0 \
	ASAN_OPTIONS=detect_leaks=0:verify_interceptors=0:log_path=$(CURDIR)/build/san-report \
	UBSAN_OPTIONS=print_stacktrace=1:log_path=$(CURDIR)/build/san-report \
	.venv/bin/python -m pytest $(TEST_SAN_ARGS); \
	status=$$?; cat build/san-report.* 2>/dev/null; exit $$status

# The subprocess tests are excluded here: dyld consumes the insertion
# variable, so their python would load TSan late — and unlike ASan there
# is no tolerate flag, a late-loaded TSan is fatal. The Linux lane
# (LD_PRELOAD inherits) covers them.
test-tsan:
	rm -f build/tsan-report.*
	DYLD_INSERT_LIBRARIES=$$(clang -print-file-name=libclang_rt.tsan_osx_dynamic.dylib) \
	TSAN_OPTIONS=halt_on_error=1:suppressions=$(CURDIR)/sanitizers/tsan.supp:log_path=$(CURDIR)/build/tsan-report \
	.venv/bin/python -m pytest $(filter-out tests/unit/test_shutdown_paths.py,$(TSAN_TESTS)); \
	status=$$?; cat build/tsan-report.* 2>/dev/null; exit $$status
else
test-san:
	rm -f build/san-report.*
	LD_PRELOAD=$$(cc -print-file-name=libasan.so) \
	ASAN_OPTIONS=detect_leaks=1:log_path=$(CURDIR)/build/san-report \
	LSAN_OPTIONS=suppressions=$(CURDIR)/sanitizers/lsan.supp \
	UBSAN_OPTIONS=print_stacktrace=1:log_path=$(CURDIR)/build/san-report \
	.venv/bin/python -m pytest $(TEST_SAN_ARGS); \
	status=$$?; cat build/san-report.* 2>/dev/null; exit $$status

test-tsan:
	rm -f build/tsan-report.*
	LD_PRELOAD=$$(cc -print-file-name=libtsan.so) \
	TSAN_OPTIONS=halt_on_error=1:suppressions=$(CURDIR)/sanitizers/tsan.supp:log_path=$(CURDIR)/build/tsan-report \
	.venv/bin/python -m pytest $(TSAN_TESTS); \
	status=$$?; cat build/tsan-report.* 2>/dev/null; exit $$status
endif

check:
	uv run python scripts/check_linkage.py

# Self-signed TLS material for the bench's TLS profile. FreeSWITCH needs
# it under three fixed names in the cert dir: agent.pem (key+cert for
# sofia-sip's transport), cafile.pem (trusted CAs — fatal if unloadable),
# and wss.pem (mod_sofia's own certificate check). Clients verify against
# cert.pem.
bench/certs/wss.pem:
	mkdir -p bench/certs
	openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
	    -keyout bench/certs/key.pem -out bench/certs/cert.pem \
	    -subj "/CN=127.0.0.1" -addext "subjectAltName=IP:127.0.0.1,DNS:localhost"
	cat bench/certs/cert.pem bench/certs/key.pem > bench/certs/wss.pem
	cp bench/certs/wss.pem bench/certs/agent.pem
	cp bench/certs/cert.pem bench/certs/cafile.pem

bench-up: bench/certs/wss.pem
	docker compose -f bench/docker-compose.yml up -d --wait

# The image runs freeswitch -nc (no console): container stdout is empty,
# the real log is the file mod_logfile writes.
bench-logs:
	docker exec baresip-bench-freeswitch tail -F /var/log/freeswitch/freeswitch.log

bench-down:
	docker compose -f bench/docker-compose.yml down
