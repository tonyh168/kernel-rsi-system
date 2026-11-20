# An intentionally small, editable workload used to test the Driver itself.
# The harness is outside the candidate worktree; the agent can edit this file.
WORK_FACTOR = 4


def run_kernel(values):
    result = []
    for _ in range(WORK_FACTOR):
        result = [value * 2 for value in values]
    return result
