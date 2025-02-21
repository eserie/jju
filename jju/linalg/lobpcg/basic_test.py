from functools import partial

import jax
import jax.numpy as jnp
import numpy as np
from absl.testing import absltest
from jax import test_util as jtu
from jax.config import config

from jju.linalg.custom_gradients import eigh_partial_rev
from jju.linalg.lobpcg.basic import lobpcg
from jju.sparse import coo, csr
from jju.test_utils import (
    coo_components,
    csr_components,
    random_spd_coo,
    random_spd_csr,
)
from jju.types import as_array_fun
from jju.utils import standardize_eigenvector_signs, symmetrize

config.parse_flags_with_absl()
config.update("jax_enable_x64", True)


@partial(jax.jit, backend="cpu", static_argnums=2)
def eigh_general(A, B, largest: bool):
    if B is None:
        w, v = jnp.linalg.eigh(A)

        def B(x):
            return x

    else:
        w, v = jnp.linalg.eig(jnp.linalg.solve(B, A))
        w = w.real
        v = v.real
        i = jnp.argsort(w)
        w = w[i]
        v = v[:, i]
        B = as_array_fun(B)
    if largest:
        w = w[-1::-1]
        v = v[:, -1::-1]

    norm2 = jax.vmap(lambda vi: (vi.conj() @ B(vi)).real, in_axes=1)(v)
    norm = jnp.sqrt(norm2)
    v = v / norm
    v = standardize_eigenvector_signs(v)
    return w, v


class BasicLobpcgTest(jtu.JaxTestCase):
    def test_eigh_general(self):
        m = 50
        dtype = np.float64
        largest = False
        A = random_spd_coo(m, dtype=dtype).todense()
        B = random_spd_coo(m, dtype=dtype, seed=1).todense()

        w, v = eigh_general(A, B, largest)
        self.assertAllClose(A @ v, B @ v * w, rtol=1e-6)
        self.assertAllClose(v.T @ B @ v, jnp.eye(m), rtol=1e-6, atol=1e-8)

    def test_lobpcg(self):
        m = 50
        k = 10
        dtype = np.float64
        A = random_spd_coo(m, dtype=dtype)
        rng = np.random.default_rng(0)
        X0 = rng.uniform(size=(m, k)).astype(dtype)

        B = None
        iK = None

        E_expected, X_expected = eigh_general(A.todense(), B, False)
        E_expected = E_expected[:k]
        X_expected = X_expected[:, :k]

        data, row, col, shape = coo_components(A.tocoo())
        A_coo = coo.matmul_fun(
            jnp.asarray(data),
            jnp.asarray(row),
            jnp.asarray(col),
            jnp.zeros(((shape[0],))),
        )
        data, indices, indptr, _ = csr_components(A.tocsr())
        A_csr = csr.matmul_fun(
            jnp.asarray(data), jnp.asarray(indices), jnp.asarray(indptr)
        )
        for A_fun in (jnp.asarray(A.todense()), A_coo, A_csr):
            E_actual, X_actual = lobpcg(
                A=A_fun, B=B, X0=X0, iK=iK, largest=False, max_iters=200
            )
            X_actual = standardize_eigenvector_signs(X_actual)
            self.assertAllClose(E_expected, E_actual, rtol=1e-8, atol=1e-10)
            self.assertAllClose(X_expected, X_actual, rtol=1e-4, atol=1e-10)

    def test_lobpcg_vjp(self):
        m = 50
        k = 10
        largest = False
        dtype = np.float64

        def lobpcg_simple(A, X0, largest, k):
            A = symmetrize(A)
            w, v = lobpcg(A, X0, largest=largest, k=k)
            v = standardize_eigenvector_signs(v)
            return w, v

        def lobpcg_fwd(A, X0, largest, k):
            w, v = lobpcg_simple(A, X0, largest, k)
            return (w, v), (w, v, A)

        def lobpcg_rev(res, g):
            grad_w, grad_v = g
            w, v, a = res
            x0 = jax.random.normal(jax.random.PRNGKey(0), shape=v.shape, dtype=v.dtype)
            grad_a, x0 = eigh_partial_rev(grad_w, grad_v, w, v, a, x0)
            grad_a = symmetrize(grad_a)
            return grad_a, None, None, None

        lobpcg_fun = jax.custom_vjp(lobpcg_simple)
        lobpcg_fun.defvjp(lobpcg_fwd, lobpcg_rev)

        A = random_spd_coo(m, dtype=dtype).todense()
        rng = np.random.default_rng(0)
        X0 = rng.uniform(size=(m, k)).astype(dtype)
        jtu.check_grads(
            partial(lobpcg_fun, X0=X0, largest=largest, k=k),
            (A,),
            order=1,
            modes=["rev"],
            rtol=1e-3,
        )

    def test_lobpcg_coo_vjp(self):
        m = 50
        k = 10
        largest = False
        dtype = np.float64

        def lobpcg_coo(data, row, col, X0, largest, k):
            size = X0.shape[0]
            data = coo.symmetrize(data, row, col, size)
            A = coo.matmul_fun(data, row, col, jnp.zeros((size,)))
            w, v = lobpcg(A, X0, largest=largest, k=k)
            v = standardize_eigenvector_signs(v)
            return w, v

        def lobpcg_fwd(data, row, col, X0, largest, k):
            w, v = lobpcg_coo(data, row, col, X0, largest, k)
            return (w, v), (w, v, data, row, col)

        def lobpcg_rev(res, g):
            grad_w, grad_v = g
            w, v, data, row, col = res
            size = v.shape[0]
            A = coo.matmul_fun(data, row, col, jnp.zeros((size,)))
            x0 = jax.random.normal(jax.random.PRNGKey(0), shape=v.shape, dtype=v.dtype)
            grad_data, x0 = eigh_partial_rev(
                grad_w, grad_v, w, v, A, x0, outer_impl=coo.masked_outer_fun(row, col)
            )
            grad_data = coo.symmetrize(grad_data, row, col, size)
            return grad_data, None, None, None, None, None

        lobpcg_fun = jax.custom_vjp(lobpcg_coo)
        lobpcg_fun.defvjp(lobpcg_fwd, lobpcg_rev)

        rng = np.random.default_rng(0)
        A = random_spd_coo(m, sparsity=0.1, dtype=dtype)
        data, row, col, _ = coo_components(A)

        X0 = rng.uniform(size=(m, k)).astype(dtype)
        jtu.check_grads(
            partial(lobpcg_fun, row=row, col=col, X0=X0, largest=largest, k=k),
            (data,),
            order=1,
            modes=["rev"],
            rtol=2e-3,
        )

    def test_lobpcg_csr_vjp(self):
        m = 50
        k = 10
        largest = False
        dtype = np.float64

        def lobpcg_csr(data, indices, indptr, X0, largest, k):
            data = csr.symmetrize(data, indices)
            A = csr.matmul_fun(data, indices, indptr)
            w, v = lobpcg(A, X0, largest=largest, k=k)
            v = standardize_eigenvector_signs(v)
            return w, v

        def lobpcg_fwd(data, indices, indptr, X0, largest, k):
            w, v = lobpcg_csr(data, indices, indptr, X0, largest, k)
            return (w, v), (w, v, data, indices, indptr)

        def lobpcg_rev(res, g):
            grad_w, grad_v = g
            w, v, data, indices, indptr = res
            A = csr.matmul_fun(data, indices, indptr)
            x0 = jax.random.normal(jax.random.PRNGKey(0), shape=v.shape, dtype=v.dtype)
            grad_data, x0 = eigh_partial_rev(
                grad_w,
                grad_v,
                w,
                v,
                A,
                x0,
                outer_impl=csr.masked_outer_fun(indices, indptr),
            )
            grad_data = csr.symmetrize(grad_data, indices)
            return grad_data, None, None, None, None, None

        lobpcg_fun = jax.custom_vjp(lobpcg_csr)
        lobpcg_fun.defvjp(lobpcg_fwd, lobpcg_rev)

        rng = np.random.default_rng(0)
        A = random_spd_csr(m, sparsity=0.1, dtype=dtype)
        data, indices, indptr, _ = csr_components(A)

        X0 = rng.uniform(size=(m, k)).astype(dtype)
        jtu.check_grads(
            partial(
                lobpcg_fun, indices=indices, indptr=indptr, X0=X0, largest=largest, k=k
            ),
            (data,),
            order=1,
            modes=["rev"],
            rtol=2e-3,
        )


if __name__ == "__main__":
    # BasicLobpcgTest().test_eigh_general()
    # BasicLobpcgTest().test_lobpcg()
    # BasicLobpcgTest().test_lobpcg_coo_vjp()
    # BasicLobpcgTest().test_lobpcg_csr_vjp()
    # print("Good!")
    absltest.main(testLoader=jtu.JaxTestLoader())
