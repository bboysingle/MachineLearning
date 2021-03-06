from Errors import *
from Basic.Optimizers import *
from Util import Timing

try:
    from Basic.CFunc.core import col2im_6d_cython
except ImportError:
    print("Cython codes are not compiled, naive cnn bp algorithm will be used.")
    col2im_6d_cython = None


# Abstract Layers

class Layer(metaclass=ABCMeta):

    LayerTiming = Timing()

    def __init__(self, shape):
        """
        :param shape: shape[0] = units of previous layer
                      shape[1] = units of current layer (self)
        """
        self._shape = shape
        self.parent = None
        self.child = None
        self.is_fc = False
        self.is_fc_base = False
        self.is_last_root = False
        self.is_sub_layer = False
        self._last_sub_layer = None

    def feed_timing(self, timing):
        if isinstance(timing, Timing):
            self.LayerTiming = timing

    @property
    def name(self):
        return str(self)

    @property
    def shape(self):
        return self._shape

    @shape.setter
    def shape(self, value):
        self._shape = value

    @property
    def params(self):
        return self._shape,

    @property
    def special_params(self):
        return

    def set_special_params(self, dic):
        for key, value in dic.items():
            setattr(self, key, value)

    @property
    def root(self):
        return self

    @root.setter
    def root(self, value):
        raise BuildLayerError("Setting Layer's root is not permitted")

    @property
    def last_sub_layer(self):
        _child = self.child
        if not _child:
            return None
        while _child.child:
            _child = _child.child
        return _child

    @last_sub_layer.setter
    def last_sub_layer(self, value):
            self._last_sub_layer = value

    # Core

    def derivative(self, y, delta=None):
        return self._derivative(y, delta)

    @LayerTiming.timeit(level=1, prefix="[Core] ")
    def activate(self, x, w, bias=None, predict=False):
        if self.is_fc:
            x = x.reshape(x.shape[0], -1)
        if self.is_sub_layer:
            if bias is None:
                return self._activate(x, predict)
            return self._activate(x + bias, predict)
        if bias is None:
            return self._activate(x.dot(w), predict)
        return self._activate(x.dot(w) + bias, predict)

    @LayerTiming.timeit(level=1, prefix="[Core] ")
    def bp(self, y, w, prev_delta):
        if self.child is not None and isinstance(self.child, SubLayer):
            if not isinstance(self, SubLayer):
                return prev_delta
            return self._derivative(y, prev_delta)
        if isinstance(self, SubLayer):
            return self._derivative(y, prev_delta.dot(w.T) * self._root.derivative(y))
        return prev_delta.dot(w.T) * self._derivative(y)

    @abstractmethod
    def _activate(self, x, predict):
        pass

    @abstractmethod
    def _derivative(self, y, delta=None):
        pass

    # Util

    @staticmethod
    @LayerTiming.timeit(level=2, prefix="[Core Util] ")
    def safe_exp(y):
        return np.exp(y - np.max(y, axis=1, keepdims=True))

    def __str__(self):
        return self.__class__.__name__

    def __repr__(self):
        return str(self)


class SubLayer(Layer):

    def __init__(self, parent, shape):
        Layer.__init__(self, shape)
        self.parent = parent
        parent.child = self
        self._root = None
        self.description = ""

    @property
    def root(self):
        _parent = self.parent
        while _parent.parent:
            _parent = _parent.parent
        return _parent

    @root.setter
    def root(self, value):
        self._root = value

    def _activate(self, x, predict):
        raise NotImplementedError("Please implement activation function for " + self.name)

    def _derivative(self, y, delta=None):
        raise NotImplementedError("Please implement derivative function for " + self.name)


class ConvLayer(Layer):

    LayerTiming = Timing()

    def __init__(self, shape, stride=1, padding=0):
        """
        :param shape:    shape[0] = shape of previous layer           c x h x w
                         shape[1] = shape of current layer's weight   f x c x h x w
        :param stride:   stride
        :param padding:  zero-padding
        """
        Layer.__init__(self, shape)
        self._stride, self._padding = stride, padding
        if len(shape) == 1:
            self.n_channels, self.n_filters, self.out_h, self.out_w = None, None, None, None
        else:
            self.feed_shape(shape)
        self.x_cache, self.x_col_cache, self.w_cache = None, None, None

    def feed_shape(self, shape):
        self._shape = shape
        self.n_channels, height, width = shape[0]
        self.n_filters, filter_height, filter_width = shape[1]
        full_height, full_width = width + 2 * self._padding, height + 2 * self._padding
        if (
            (full_height - filter_height) % self._stride != 0 or
            (full_width - filter_width) % self._stride != 0
        ):
            raise BuildLayerError(
                "Weight shape does not work, "
                "shape: {} - stride: {} - padding: {} not compatible with {}".format(
                    self._shape[1][1:], self._stride, self._padding, (height, width)
                ))
        self.out_h = int((height + 2 * self._padding - filter_height) / self._stride) + 1
        self.out_w = int((width + 2 * self._padding - filter_width) / self._stride) + 1

    @property
    def params(self):
        return self._shape, self._stride, self._padding

    @property
    def stride(self):
        return self._stride

    @property
    def padding(self):
        return self._padding

    def _activate(self, x, predict):
        raise NotImplementedError("Please implement activation function for " + self.name)

    def _derivative(self, y, delta=None):
        raise NotImplementedError("Please implement derivative function for " + self.name)


class ConvPoolLayer(ConvLayer):

    LayerTiming = Timing()

    def __init__(self, shape, stride=1, padding=0):
        """
        :param shape:    shape[0] = shape of previous layer           c x h x w
                         shape[1] = shape of pool window
        :param stride:   stride
        :param padding:  zero-padding
        """
        Layer.__init__(self, shape)
        self._stride, self._padding = stride, padding
        if len(shape) == 1:
            self.n_channels, self.n_filters, self.out_h, self.out_w = None, None, None, None
        else:
            self.feed_shape(shape)
        self._pool_cache, self.inner_weight = {}, None

    def feed_shape(self, shape):
        self._shape = shape
        self.n_channels, height, width = shape[0]
        pool_height, pool_width = shape[1]
        self.n_filters = self.n_channels
        full_height, full_width = width + 2 * self._padding, height + 2 * self._padding
        if (
            (full_height - pool_height) % self._stride != 0 or
            (full_width - pool_width) % self._stride != 0
        ):
            raise BuildLayerError(
                "Pool shape does not work, "
                "shape: {} - stride: {} - padding: {} not compatible with {}".format(
                    self._shape[1], self._stride, self._padding, (height, width)
                ))
        self.out_h = int((height - pool_height) / self._stride) + 1
        self.out_w = int((width - pool_width) / self._stride) + 1

    @LayerTiming.timeit(level=1, prefix="[Core] ")
    def activate(self, x, w, bias=None, predict=False):
        return self._activate(x, w, bias, predict)

    def _activate(self, x, *args):
        raise NotImplementedError("Please implement activation function for " + self.name)

    def _derivative(self, y, *args):
        raise NotImplementedError("Please implement derivative function for " + self.name)

    @LayerTiming.timeit(level=1, prefix="[Core] ")
    def bp(self, y, w, prev_delta):
        return self._derivative(y, w, prev_delta)


class ConvMeta(type):

    def __new__(mcs, *args, **kwargs):
        name, bases, attr = args[:3]
        conv_layer, layer = bases

        def __init__(self, shape, stride=1, padding=0):
            conv_layer.__init__(self, shape, stride, padding)

        def _activate(self, x, w, bias, predict):
            self.x_cache, self.w_cache = x, w
            n, n_channels, height, width = x.shape
            n_filters, _, filter_height, filter_width = w.shape

            p, sd = self._padding, self._stride
            x_padded = np.pad(x, ((0, 0), (0, 0), (p, p), (p, p)), mode='constant')

            height += 2 * p
            width += 2 * p

            shape = (n_channels, filter_height, filter_width, n, self.out_h, self.out_w)
            strides = (height * width, width, 1, n_channels * height * width, sd * width, sd)
            strides = x.itemsize * np.array(strides)
            x_cols = np.lib.stride_tricks.as_strided(x_padded, shape=shape, strides=strides).reshape(
                n_channels * filter_height * filter_width, n * self.out_h * self.out_w)
            self.x_col_cache = x_cols

            if bias is None:
                res = w.reshape(n_filters, -1).dot(x_cols)
            else:
                res = w.reshape(n_filters, -1).dot(x_cols) + bias.reshape(-1, 1)
            res.shape = (n_filters, n, self.out_h, self.out_w)
            return layer._activate(self, res.transpose(1, 0, 2, 3), predict)

        def _derivative(self, y, w, prev_delta):
            n = len(y)
            n_channels, height, width = self._shape[0]
            n_filters, filter_height, filter_width = self._shape[1]

            p, sd = self._padding, self._stride
            if isinstance(prev_delta, tuple):
                prev_delta = prev_delta[0]

            __derivative = self.LayerTiming.timeit(level=1, name="bp", cls_name=name, prefix="[Core] ")(
                layer._derivative)
            if self.is_fc_base:
                delta = __derivative(self, y) * prev_delta.dot(w.T).reshape(y.shape)
            else:
                delta = __derivative(self, y) * prev_delta

            dw = delta.transpose(1, 0, 2, 3).reshape(n_filters, -1).dot(
                self.x_col_cache.T).reshape(self.w_cache.shape)
            db = np.sum(delta, axis=(0, 2, 3))

            n_filters, _, filter_height, filter_width = self.w_cache.shape
            _, _, out_h, out_w = delta.shape

            if col2im_6d_cython is not None:
                dx_cols = self.w_cache.reshape(n_filters, -1).T.dot(delta.transpose(1, 0, 2, 3).reshape(n_filters, -1))
                dx_cols.shape = (n_channels, filter_height, filter_width, n, out_h, out_w)
                dx = col2im_6d_cython(
                    dx_cols, n, n_channels, height, width, filter_height, filter_width, self._padding, self._stride)
            else:
                dx_padded = np.zeros((n, n_channels, height + 2 * p, width + 2 * p))
                for i in range(n):
                    for f in range(n_filters):
                        for j in range(self.out_h):
                            for k in range(self.out_w):
                                dx_padded[i, :, j * sd:filter_height + j * sd, k * sd:filter_width + k * sd] += (
                                    self.w_cache[f] * delta[i, f, j, k])
                dx = dx_padded[:, :, p:-p, p:-p] if p > 0 else dx_padded
            return dx, dw, db

        def activate(self, x, w, bias=None, predict=False):
            return self.LayerTiming.timeit(level=1, name="activate", cls_name=name, prefix="[Core] ")(
                _activate)(self, x, w, bias, predict)

        def bp(self, y, w, prev_delta):
            return self.LayerTiming.timeit(level=1, name="bp", cls_name=name, prefix="[Core] ")(
                _derivative)(self, y, w, prev_delta)

        for key, value in locals().items():
            if str(value).find("function") >= 0 or str(value).find("property"):
                attr[key] = value

        return type(name, bases, attr)


class ConvSubMeta(type):

    def __new__(mcs, *args, **kwargs):
        name, bases, attr = args[:3]
        conv_layer, sub_layer = bases

        def __init__(self, parent, shape, *_args, **_kwargs):
            conv_layer.__init__(self, parent.shape, parent.stride, parent.padding)
            self.out_h, self.out_w = parent.out_h, parent.out_w
            sub_layer.__init__(self, parent, shape, *_args, **_kwargs)
            if name == "ConvNorm":
                self.gamma, self.beta = np.ones(self.n_filters), np.zeros(self.n_filters)
                self.init_optimizers()

        def _activate(self, x, predict):
            n, n_channels, height, width = x.shape
            x_new = x.transpose(0, 2, 3, 1).reshape(-1, n_channels)
            out = sub_layer._activate(self, x_new, predict)
            return out.reshape(n, height, width, n_channels).transpose(0, 3, 1, 2)

        def _derivative(self, y, w, delta=None):
            if self.is_fc_base:
                delta = delta.dot(w.T).reshape(y.shape)
            n, n_channels, height, width = delta.shape
            delta_new = delta.transpose(0, 2, 3, 1).reshape(-1, n_channels)
            dx = sub_layer._derivative(self, None, delta_new)
            return dx.reshape(n, height, width, n_channels).transpose(0, 3, 1, 2)

        def activate(self, x, w, bias=None, predict=False):
            return self.LayerTiming.timeit(level=1, name="activate", cls_name=name, prefix="[Core] ")(
                _activate)(self, x, predict)

        def bp(self, y, w, prev_delta):
            if isinstance(prev_delta, tuple):
                prev_delta = prev_delta[0]
            return self.LayerTiming.timeit(level=1, name="bp", cls_name=name, prefix="[Core] ")(
                _derivative)(self, y, w, prev_delta)

        for key, value in locals().items():
            if str(value).find("function") >= 0 or str(value).find("property"):
                attr[key] = value

        return type(name, bases, attr)


class ConvLayerMeta(ABCMeta, ConvMeta):
    pass


class ConvSubLayerMeta(ABCMeta, ConvSubMeta):
    pass


# Activation Layers

class Tanh(Layer):

    def _activate(self, x, predict):
        return np.tanh(x)

    def _derivative(self, y, delta=None):
        return 1 - y ** 2


class Sigmoid(Layer):

    def _activate(self, x, predict):
        return 1 / (1 + np.exp(-x))

    def _derivative(self, y, delta=None):
        return y * (1 - y)


class ELU(Layer):

    def _activate(self, x, predict):
        _rs, _rs0 = x.copy(), x < 0
        _rs[_rs0] = np.exp(_rs[_rs0]) - 1
        return _rs

    def _derivative(self, y, delta=None):
        _rs, _arg0 = np.zeros(y.shape), y < 0
        _rs[_arg0], _rs[~_arg0] = y[_arg0] + 1, 1
        return _rs


class ReLU(Layer):

    def _activate(self, x, predict):
        return np.maximum(0, x)

    def _derivative(self, y, delta=None):
        return y > 0


class Softplus(Layer):

    def _activate(self, x, predict):
        return np.log(1 + np.exp(x))

    def _derivative(self, y, delta=None):
        return 1 / (1 + 1 / (np.exp(y) - 1))


class Identical(Layer):

    def _activate(self, x, predict):
        return x

    def _derivative(self, y, delta=None):
        return 1


class Softmax(Layer):

    def _activate(self, x, predict):
        exp_y = Layer.safe_exp(x)
        return exp_y / np.sum(exp_y, axis=1, keepdims=True)

    def _derivative(self, y, delta=None):
        return y * (1 - y)


# Convolution Layers

class ConvTanh(ConvLayer, Tanh, metaclass=ConvLayerMeta):
    pass


class ConvSigmoid(ConvLayer, Sigmoid, metaclass=ConvLayerMeta):
    pass


class ConvELU(ConvLayer, ELU, metaclass=ConvLayerMeta):
    pass


class ConvReLU(ConvLayer, ReLU, metaclass=ConvLayerMeta):
    pass


class ConvSoftplus(ConvLayer, Softplus, metaclass=ConvLayerMeta):
    pass


class ConvIdentical(ConvLayer, Identical, metaclass=ConvLayerMeta):
    pass


class ConvSoftmax(ConvLayer, Softmax, metaclass=ConvLayerMeta):
    pass


# Pooling Layers

class MaxPool(ConvPoolLayer):

    def _activate(self, x, *args):
        self.x_cache = x
        sd = self._stride
        n, n_channels, height, width = x.shape
        pool_height, pool_width = self._shape[1]
        same_size = pool_height == pool_width == sd
        tiles = height % pool_height == 0 and width % pool_width == 0
        if same_size and tiles:
            x_reshaped = x.reshape(n, n_channels, int(height / pool_height), pool_height,
                                   int(width / pool_width), pool_width)
            self._pool_cache["x_reshaped"] = x_reshaped
            out = x_reshaped.max(axis=3).max(axis=4)
            self._pool_cache["method"] = "reshape"
        else:
            out = np.zeros((n, n_channels, self.out_h, self.out_w))
            for i in range(n):
                for j in range(n_channels):
                    for k in range(self.out_h):
                        for l in range(self.out_w):
                            window = x[i, j, k * sd:pool_height + k * sd, l * sd:pool_width + l * sd]
                            out[i, j, k, l] = np.max(window)
            self._pool_cache["method"] = "original"
        return out

    def _derivative(self, y, *args):
        w, prev_delta = args
        if isinstance(prev_delta, tuple):
            prev_delta = prev_delta[0]
        if self.is_fc_base:
            delta = prev_delta.dot(w.T).reshape(y.shape)
        else:
            delta = prev_delta
        method = self._pool_cache["method"]
        if method == "reshape":
            x_reshaped_cache = self._pool_cache["x_reshaped"]
            dx_reshaped = np.zeros_like(x_reshaped_cache)
            out_newaxis = y[:, :, :, None, :, None]
            mask = (x_reshaped_cache == out_newaxis)
            dout_newaxis = delta[:, :, :, None, :, None]
            dout_broadcast, _ = np.broadcast_arrays(dout_newaxis, dx_reshaped)
            dx_reshaped[mask] = dout_broadcast[mask]
            dx_reshaped /= np.sum(mask, axis=(3, 5), keepdims=True)
            dx = dx_reshaped.reshape(self.x_cache.shape)
        elif method == "original":
            sd = self._stride
            dx = np.zeros_like(self.x_cache)
            n, n_channels, _, _ = self.x_cache.shape
            pool_height, pool_width = self._shape[1]
            for i in range(n):
                for j in range(n_channels):
                    for k in range(self.out_h):
                        for l in range(self.out_w):
                            window = self.x_cache[i, j, k*sd:pool_height+k*sd, l*sd:pool_width+l*sd]
                            dx[i, j, k*sd:pool_height+k*sd, l*sd:pool_width+l*sd] = (
                                window == np.max(window)) * delta[i, j, k, l]
        else:
            raise LayerError("Undefined pooling method '{}' found".format(method))
        return dx, None, None


# Special Layer

class Dropout(SubLayer):

    def __init__(self, parent, shape, prob=0.5):
        if prob < 0 or prob >= 1:
            raise BuildLayerError("Probability of Dropout should be a positive float smaller than 1")
        SubLayer.__init__(self, parent, shape)
        self._prob = prob
        self._prob_inv = 1 / (1 - prob)
        self.description = "(Drop prob: {})".format(prob)

    def _activate(self, x, predict):
        if not predict:
            _diag = np.diag(np.random.random(x.shape[1]) >= self._prob) * self._prob_inv
        else:
            _diag = np.eye(x.shape[1])
        return x.dot(_diag)

    def _derivative(self, y, delta=None):
        return self._prob_inv * delta


class Normalize(SubLayer):

    def __init__(self, parent, shape, lr=0.001, eps=1e-8, momentum=0.9, optimizers=None):
        SubLayer.__init__(self, parent, shape)
        self.sample_mean, self.sample_var = None, None
        self.running_mean, self.running_var = None, None
        self.x_cache, self.x_normalized_cache = None, None
        self._lr, self._eps = lr, eps
        if optimizers is None:
            self._g_optimizer, self._b_optimizer = Adam(self._lr), Adam(self._lr)
        else:
            self._g_optimizer, self._b_optimizer = optimizers
        self.gamma, self.beta = np.ones(self.shape[1]), np.zeros(self.shape[1])
        self._momentum = momentum
        self.init_optimizers()
        self.description = "(lr: {}, eps: {}, momentum: {}, optimizer: ({}, {}))".format(
            lr, eps, momentum, self._g_optimizer.name, self._b_optimizer.name
        )

    @property
    def params(self):
        return self._lr, self._eps, self._momentum, (self._g_optimizer.name, self._b_optimizer.name)

    @property
    def special_params(self):
        return {
            "gamma": self.gamma, "beta": self.beta,
            "running_mean": self.running_mean, "running_var": self.running_var,
            "_g_optimizer": self._g_optimizer, "_b_optimizer": self._b_optimizer
        }

    def init_optimizers(self):
        _opt_fac = OptFactory()
        if not isinstance(self._g_optimizer, Optimizers):
            self._g_optimizer = _opt_fac.get_optimizer_by_name(
                self._g_optimizer, None, self.LayerTiming, self._lr, None
            )
        if not isinstance(self._b_optimizer, Optimizers):
            self._b_optimizer = _opt_fac.get_optimizer_by_name(
                self._b_optimizer, None, self.LayerTiming, self._lr, None
            )
        self._g_optimizer.feed_variables([self.gamma])
        self._b_optimizer.feed_variables([self.beta])

    def _activate(self, x, predict):
        if self.running_mean is None or self.running_var is None:
            self.running_mean, self.running_var = np.zeros(x.shape[1]), np.zeros(x.shape[1])
        if not predict:
            self.sample_mean = np.mean(x, axis=0, keepdims=True)
            self.sample_var = np.var(x, axis=0, keepdims=True)
            x_normalized = (x - self.sample_mean) / np.sqrt(self.sample_var + self._eps)
            self.x_cache, self.x_normalized_cache = x, x_normalized
            out = self.gamma * x_normalized + self.beta
            self.running_mean = self._momentum * self.running_mean + (1 - self._momentum) * self.sample_mean
            self.running_var = self._momentum * self.running_var + (1 - self._momentum) * self.sample_var
        else:
            x_normalized = (x - self.running_mean) / np.sqrt(self.running_var + self._eps)
            out = self.gamma * x_normalized + self.beta
        return out

    def _derivative(self, y, delta=None):
        n, d = self.x_cache.shape
        dx_normalized = delta * self.gamma
        x_mu = self.x_cache - self.sample_mean
        sample_std_inv = 1.0 / np.sqrt(self.sample_var + self._eps)
        ds_var = -0.5 * np.sum(dx_normalized * x_mu, axis=0, keepdims=True) * sample_std_inv ** 3
        ds_mean = (-1.0 * np.sum(dx_normalized * sample_std_inv, axis=0, keepdims=True) - 2.0 *
                   ds_var * np.mean(x_mu, axis=0, keepdims=True))
        dx1 = dx_normalized * sample_std_inv
        dx2 = 2.0 / n * ds_var * x_mu
        dx = dx1 + dx2 + 1.0 / n * ds_mean
        dg = -np.sum(delta * self.x_normalized_cache, axis=0)
        db = -np.sum(delta, axis=0)
        self.gamma += self._g_optimizer.run(0, dg)
        self.beta += self._b_optimizer.run(0, db)
        self._g_optimizer.update(); self._b_optimizer.update()
        return delta - dx


class ConvDrop(ConvLayer, Dropout, metaclass=ConvSubLayerMeta):
    pass


class ConvNorm(ConvLayer, Normalize, metaclass=ConvSubLayerMeta):
    pass


# Cost Layer

class CostLayer(SubLayer):

    # Optimization
    _batch_range = None

    def __init__(self, parent, shape, cost_function="MSE"):

        SubLayer.__init__(self, parent, shape)
        self._available_cost_functions = {
            "MSE": CostLayer._mse,
            "SVM": CostLayer._svm,
            "Cross Entropy": CostLayer._cross_entropy,
            "Log Likelihood": CostLayer._log_likelihood
        }

        if cost_function not in self._available_cost_functions:
            raise LayerError("Cost function '{}' not implemented".format(cost_function))
        self._cost_function_name = cost_function
        self._cost_function = self._available_cost_functions[cost_function]

    def _activate(self, x, predict):
        return x

    def _derivative(self, y, delta=None):
        raise LayerError("derivative function should not be called in CostLayer")

    def bp_first(self, y, y_pred):
        if self._root.name == "Sigmoid" and self.cost_function == "Cross Entropy":
            return y * (1 - y_pred) - (1 - y) * y_pred
        if self.cost_function == "Log Likelihood":
            return -self._cost_function(y, y_pred) / 4
        return -self._cost_function(y, y_pred) * self._root.derivative(y_pred)

    @property
    def calculate(self):
        return lambda y, y_pred: self._cost_function(y, y_pred, False)

    @property
    def cost_function(self):
        return self._cost_function_name

    @cost_function.setter
    def cost_function(self, value):
        if value not in self._available_cost_functions:
            raise LayerError("'{}' is not implemented".format(value))
        self._cost_function_name = value
        self._cost_function = self._available_cost_functions[value]

    def set_cost_function_derivative(self, func, name=None):
        name = "Custom Cost Function" if name is None else name
        self._cost_function_name = name
        self._cost_function = func

    # Cost Functions

    @staticmethod
    def _mse(y, y_pred, diff=True):
        if diff:
            return -y + y_pred
        assert_string = "y or y_pred should be np.ndarray in cost function"
        assert isinstance(y, np.ndarray) or isinstance(y_pred, np.ndarray), assert_string
        return 0.5 * np.average((y - y_pred) ** 2)

    @staticmethod
    def _svm(y, y_pred, diff=True):
        n, y = y_pred.shape[0], np.argmax(y, axis=1)
        correct_class_scores = y_pred[np.arange(n), y]
        margins = np.maximum(0, y_pred - correct_class_scores[:, None] + 1.0)
        margins[np.arange(n), y] = 0
        loss = np.sum(margins) / n
        num_pos = np.sum(margins > 0, axis=1)
        if not diff:
            return loss
        dx = np.zeros_like(y_pred)
        dx[margins > 0] = 1
        dx[np.arange(n), y] -= num_pos
        dx /= n
        return dx

    @staticmethod
    def _cross_entropy(y, y_pred, diff=True):
        if diff:
            return -y / y_pred + (1 - y) / (1 - y_pred)
        assert_string = "y or y_pred should be np.ndarray in cost function"
        assert isinstance(y, np.ndarray) or isinstance(y_pred, np.ndarray), assert_string
        return np.average(-y * np.log(y_pred) - (1 - y) * np.log(1 - y_pred))

    @classmethod
    def _log_likelihood(cls, y, y_pred, diff=True, eps=1e-8):
        if cls._batch_range is None:
            cls._batch_range = np.arange(len(y_pred))
        y_arg_max = np.argmax(y, axis=1)
        if diff:
            y_pred = y_pred.copy()
            y_pred[cls._batch_range, y_arg_max] -= 1
            return y_pred
        return np.sum(-np.log(y_pred[range(len(y_pred)), y_arg_max] + eps)) / len(y)

    def __str__(self):
        return self._cost_function_name

    
# Factory

class LayerFactory:
    
    available_root_layers = {
        "Tanh": Tanh, "Sigmoid": Sigmoid,
        "ELU": ELU, "ReLU": ReLU, "Softplus": Softplus,
        "Softmax": Softmax,
        "Identical": Identical,
        "ConvTanh": ConvTanh, "ConvSigmoid": ConvSigmoid,
        "ConvELU": ConvELU, "ConvReLU": ConvReLU, "ConvSoftplus": ConvSoftplus,
        "ConvSoftmax": ConvSoftmax,
        "ConvIdentical": ConvIdentical,
        "MaxPool": MaxPool
    }
    available_sub_layers = {
        "Dropout", "Normalize", "ConvNorm", "ConvDrop",
        "MSE", "SVM", "Cross Entropy", "Log Likelihood"
    }
    available_cost_functions = {
        "MSE", "SVM", "Cross Entropy", "Log Likelihood"
    }
    available_special_layers = {
        "Dropout": Dropout,
        "Normalize": Normalize,
        "ConvDrop": ConvDrop,
        "ConvNorm": ConvNorm
    }
    special_layer_default_params = {
        "Dropout": (0.5, ),
        "Normalize": (0.01, 1e-8, 0.9),
        "ConvDrop": (0.5, ),
        "ConvNorm": (0.001, 1e-8, 0.9)
    }

    def handle_str_main_layers(self, name, *args, **kwargs):
        if name not in self.available_sub_layers:
            if name in self.available_root_layers:
                name = self.available_root_layers[name]
            else:
                raise BuildNetworkError("Undefined layer '{}' found".format(name))
            return name(*args, **kwargs)
        return None
    
    def get_layer_by_name(self, name, parent, current_dimension, *args, **kwargs):
        _layer = self.handle_str_main_layers(name, *args, **kwargs)
        if _layer:
            return _layer, None
        _current, _next = parent.shape[1], current_dimension
        if name in self.available_cost_functions:
            _layer = CostLayer(parent, (_current, _next), name)
        else:
            layer_param = self.special_layer_default_params[name]
            _layer = self.available_special_layers[name]
            if args or kwargs:
                _layer = _layer(parent, (_current, _next), *args, **kwargs)
            else:
                _layer = _layer(parent, (_current, _next), *layer_param)
        return _layer, (_current, _next)
