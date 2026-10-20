/*
 * msgbridge launcher.
 *
 * Claude starts MCP servers through a helper that makes the launched program
 * "responsible" for the process tree, so macOS checks Full Disk Access against
 * that program. Pointing Claude at this small binary, instead of uv, lets you
 * grant Full Disk Access to msgbridge alone rather than to every uv script.
 *
 * It stays alive as the parent (no exec), so the server and its children stay
 * covered by this binary's permission. Stdin/stdout pass straight through.
 *
 *   msgbridge-launch            run the MCP server over stdio
 *   msgbridge-launch ARGS...    run the msgbridge CLI, e.g. `msgbridge-launch sync`
 */
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

#ifndef PROJECT_DIR
#error "build with -DPROJECT_DIR=\"/path/to/msgbridge\""
#endif

extern char **environ;
static pid_t child = 0;

static void forward(int sig) {
    if (child > 0) kill(child, sig);
}

int main(int argc, char **argv) {
    const char *home = getenv("HOME");
    if (!home) {
        fprintf(stderr, "msgbridge-launch: HOME is not set\n");
        return 1;
    }
    char uv[1024], pythonpath[1024];
    snprintf(uv, sizeof uv, "%s/.local/bin/uv", home);
    snprintf(pythonpath, sizeof pythonpath, "%s/src", PROJECT_DIR);
    setenv("PYTHONPATH", pythonpath, 1);

    /* uv run --quiet --directory PROJECT python -m msgbridge.{server|cli} [ARGS...] */
    char **args = calloc(argc + 9, sizeof(char *));
    int n = 0;
    args[n++] = uv;
    args[n++] = "run";
    args[n++] = "--quiet";
    args[n++] = "--directory";
    args[n++] = PROJECT_DIR;
    args[n++] = "python";
    args[n++] = "-m";
    args[n++] = argc > 1 ? "msgbridge.cli" : "msgbridge.server";
    for (int i = 1; i < argc; i++) args[n++] = argv[i];
    args[n] = NULL;

    signal(SIGTERM, forward);
    signal(SIGINT, forward);
    signal(SIGHUP, forward);

    int err = posix_spawn(&child, uv, NULL, NULL, args, environ);
    if (err) {
        fprintf(stderr, "msgbridge-launch: could not start %s: %s\n", uv, strerror(err));
        return 1;
    }
    int status = 0;
    while (waitpid(child, &status, 0) < 0) {
        /* interrupted by a forwarded signal; keep waiting for the child */
    }
    return WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
}
