import math
import torch
import logging
import numpy as np
import torch.nn as nn
import torch.nn.functional as F
from typing import Tuple

logger = logging.getLogger(__name__)

"""
from speechbrain.nnet.CNN import Conv1d as _Conv1d
from speechbrain.nnet.normalization import BatchNorm1d as _BatchNorm1d
"""

class SincConv(nn.Module):
    """This function implements SincConv (SincNet).
    M. Ravanelli, Y. Bengio, "Speaker Recognition from raw waveform with
    SincNet", in Proc. of  SLT 2018 (https://arxiv.org/abs/1808.00158)
    Arguments
    ---------
    input_shape : tuple
        The shape of the input. Alternatively use ``in_channels``.
    in_channels : int
        The number of input channels. Alternatively use ``input_shape``.
    out_channels : int
        It is the number of output channels.
    kernel_size: int
        Kernel size of the convolutional filters.
    stride : int
        Stride factor of the convolutional filters. When the stride factor > 1,
        a decimation in time is performed.
    dilation : int
        Dilation factor of the convolutional filters.
    padding : str
        (same, valid, causal). If "valid", no padding is performed.
        If "same" and stride is 1, output shape is the same as the input shape.
        "causal" results in causal (dilated) convolutions.
    padding_mode : str
        This flag specifies the type of padding. See torch.nn documentation
        for more information.
    groups : int
        This option specifies the convolutional groups. See torch.nn
        documentation for more information.
    bias : bool
        If True, the additive bias b is adopted.
    sample_rate : int,
        Sampling rate of the input signals. It is only used for sinc_conv.
    min_low_hz : float
        Lowest possible frequency (in Hz) for a filter. It is only used for
        sinc_conv.
    min_low_hz : float
        Lowest possible value (in Hz) for a filter bandwidth.
    Example
    -------
    >>> inp_tensor = torch.rand([10, 16000])
    >>> conv = SincConv(input_shape=inp_tensor.shape, out_channels=25, kernel_size=11)
    >>> out_tensor = conv(inp_tensor)
    >>> out_tensor.shape
    torch.Size([10, 16000, 25])
    """

    def __init__(
        self,
        out_channels,
        kernel_size,
        input_shape=None,
        in_channels=None,
        stride=1,
        dilation=1,
        padding="same",
        padding_mode="reflect",
        sample_rate=16000,
        min_low_hz=50,
        min_band_hz=50,
    ):
        super().__init__()
        self.out_channels = out_channels
        self.kernel_size = kernel_size
        self.stride = stride
        self.dilation = dilation
        self.padding = padding
        self.padding_mode = padding_mode
        self.sample_rate = sample_rate
        self.min_low_hz = min_low_hz
        self.min_band_hz = min_band_hz

        # input shape inference
        if input_shape is None and in_channels is None:
            raise ValueError("Must provide one of input_shape or in_channels")

        if in_channels is None:
            in_channels = self._check_input_shape(input_shape)

        # Initialize Sinc filters
        self._init_sinc_conv()

    def forward(self, x):
        """Returns the output of the convolution.
        Arguments
        ---------
        x : torch.Tensor (batch, time, channel)
            input to convolve. 2d or 4d tensors are expected.
        """
        x = x.transpose(1, -1)
        self.device = x.device

        unsqueeze = x.ndim == 2
        if unsqueeze:
            x = x.unsqueeze(1)

        if self.padding == "same":
            x = self._manage_padding(
                x, self.kernel_size, self.dilation, self.stride
            )

        elif self.padding == "causal":
            num_pad = (self.kernel_size - 1) * self.dilation
            x = F.pad(x, (num_pad, 0))

        elif self.padding == "valid":
            pass

        else:
            raise ValueError(
                "Padding must be 'same', 'valid' or 'causal'. Got %s."
                % (self.padding)
            )

        sinc_filters = self._get_sinc_filters()

        wx = F.conv1d(
            x,
            sinc_filters,
            stride=self.stride,
            padding=0,
            dilation=self.dilation,
        )

        if unsqueeze:
            wx = wx.squeeze(1)

        wx = wx.transpose(1, -1)

        return wx

    def _check_input_shape(self, shape):
        """Checks the input shape and returns the number of input channels.
        """

        if len(shape) == 2:
            in_channels = 1
        elif len(shape) == 3:
            in_channels = 1
        else:
            raise ValueError(
                "sincconv expects 2d or 3d inputs. Got " + str(len(shape))
            )

        # Kernel size must be odd
        if self.kernel_size % 2 == 0:
            raise ValueError(
                "The field kernel size must be an odd number. Got %s."
                % (self.kernel_size)
            )
        return in_channels

    def _get_sinc_filters(self,):
        """This functions creates the sinc-filters to used for sinc-conv.
        """
        # Computing the low frequencies of the filters
        low = self.min_low_hz + torch.abs(self.low_hz_)

        # Setting minimum band and minimum freq
        high = torch.clamp(
            low + self.min_band_hz + torch.abs(self.band_hz_),
            self.min_low_hz,
            self.sample_rate / 2,
        )
        band = (high - low)[:, 0]

        # Passing from n_ to the corresponding f_times_t domain
        self.n_ = self.n_.to(self.device)
        self.window_ = self.window_.to(self.device)
        f_times_t_low = torch.matmul(low, self.n_)
        f_times_t_high = torch.matmul(high, self.n_)

        # Left part of the filters.
        band_pass_left = (
            (torch.sin(f_times_t_high) - torch.sin(f_times_t_low))
            / (self.n_ / 2)
        ) * self.window_

        # Central element of the filter
        band_pass_center = 2 * band.view(-1, 1)

        # Right part of the filter (sinc filters are symmetric)
        band_pass_right = torch.flip(band_pass_left, dims=[1])

        # Combining left, central, and right part of the filter
        band_pass = torch.cat(
            [band_pass_left, band_pass_center, band_pass_right], dim=1
        )

        # Amplitude normalization
        band_pass = band_pass / (2 * band[:, None])

        # Setting up the filter coefficients
        filters = band_pass.view(self.out_channels, 1, self.kernel_size)

        return filters

    def _init_sinc_conv(self):
        """Initializes the parameters of the sinc_conv layer."""

        # Initialize filterbanks such that they are equally spaced in Mel scale
        high_hz = self.sample_rate / 2 - (self.min_low_hz + self.min_band_hz)

        mel = torch.linspace(
            self._to_mel(self.min_low_hz),
            self._to_mel(high_hz),
            self.out_channels + 1,
        )

        hz = self._to_hz(mel)

        # Filter lower frequency and bands
        self.low_hz_ = hz[:-1].unsqueeze(1)
        self.band_hz_ = (hz[1:] - hz[:-1]).unsqueeze(1)

        # Maiking freq and bands learnable
        self.low_hz_ = nn.Parameter(self.low_hz_)
        self.band_hz_ = nn.Parameter(self.band_hz_)

        # Hamming window
        n_lin = torch.linspace(
            0, (self.kernel_size / 2) - 1, steps=int((self.kernel_size / 2))
        )
        self.window_ = 0.54 - 0.46 * torch.cos(
            2 * math.pi * n_lin / self.kernel_size
        )

        # Time axis  (only half is needed due to symmetry)
        n = (self.kernel_size - 1) / 2.0
        self.n_ = (
            2 * math.pi * torch.arange(-n, 0).view(1, -1) / self.sample_rate
        )

    def _to_mel(self, hz):
        """Converts frequency in Hz to the mel scale.
        """
        return float(2595 * torch.log10(torch.tensor(1 + hz / 700)))

    def _to_hz(self, mel):
        """Converts frequency in the mel scale to Hz.
        """
        return 700 * (10 ** (mel / 2595) - 1)

    def _manage_padding(
        self, x, kernel_size: int, dilation: int, stride: int,
    ):
        """This function performs zero-padding on the time axis
        such that their lengths is unchanged after the convolution.
        Arguments
        ---------
        x : torch.Tensor
            Input tensor.
        kernel_size : int
            Size of kernel.
        dilation : int
            Dilation used.
        stride : int
            Stride.
        """

        # Detecting input shape
        L_in = x.shape[-1]

        # Time padding
        padding = get_padding_elem(L_in, stride, kernel_size, dilation)

        # Applying padding
        x = F.pad(x, padding, mode=self.padding_mode)

        return x


class Conv1d(nn.Module):
    """This function implements 1d convolution.
    Arguments
    ---------
    out_channels : int
        It is the number of output channels.
    kernel_size : int
        Kernel size of the convolutional filters.
    input_shape : tuple
        The shape of the input. Alternatively use ``in_channels``.
    in_channels : int
        The number of input channels. Alternatively use ``input_shape``.
    stride : int
        Stride factor of the convolutional filters. When the stride factor > 1,
        a decimation in time is performed.
    dilation : int
        Dilation factor of the convolutional filters.
    padding : str
        (same, valid, causal). If "valid", no padding is performed.
        If "same" and stride is 1, output shape is the same as the input shape.
        "causal" results in causal (dilated) convolutions.
    padding_mode : str
        This flag specifies the type of padding. See torch.nn documentation
        for more information.
    skip_transpose : bool
        If False, uses batch x time x channel convention of speechbrain.
        If True, uses batch x channel x time convention.
    Example
    -------
    >>> inp_tensor = torch.rand([10, 40, 16])
    >>> cnn_1d = Conv1d(
    ...     input_shape=inp_tensor.shape, out_channels=8, kernel_size=5
    ... )
    >>> out_tensor = cnn_1d(inp_tensor)
    >>> out_tensor.shape
    torch.Size([10, 40, 8])
    """

    def __init__(
        self,
        out_channels,
        kernel_size,
        input_shape=None,
        in_channels=None,
        stride=1,
        dilation=1,
        padding="same",
        groups=1,
        bias=True,
        padding_mode="reflect",
        skip_transpose=False,
    ):
        super().__init__()
        self.kernel_size = kernel_size
        self.stride = stride
        self.dilation = dilation
        self.padding = padding
        self.padding_mode = padding_mode
        self.unsqueeze = False
        self.skip_transpose = skip_transpose

        if input_shape is None and in_channels is None:
            raise ValueError("Must provide one of input_shape or in_channels")

        if in_channels is None:
            in_channels = self._check_input_shape(input_shape)

        self.conv = nn.Conv1d(
            in_channels,
            out_channels,
            self.kernel_size,
            stride=self.stride,
            dilation=self.dilation,
            padding=0,
            groups=groups,
            bias=bias,
        )

    def forward(self, x):
        """Returns the output of the convolution.
        Arguments
        ---------
        x : torch.Tensor (batch, time, channel)
            input to convolve. 2d or 4d tensors are expected.
        """

        if not self.skip_transpose:
            x = x.transpose(1, -1)

        if self.unsqueeze:
            x = x.unsqueeze(1)

        if self.padding == "same":
            x = self._manage_padding(
                x, self.kernel_size, self.dilation, self.stride
            )

        elif self.padding == "causal":
            num_pad = (self.kernel_size - 1) * self.dilation
            x = F.pad(x, (num_pad, 0))

        elif self.padding == "valid":
            pass

        else:
            raise ValueError(
                "Padding must be 'same', 'valid' or 'causal'. Got "
                + self.padding
            )

        wx = self.conv(x)

        if self.unsqueeze:
            wx = wx.squeeze(1)

        if not self.skip_transpose:
            wx = wx.transpose(1, -1)

        return wx

    def _manage_padding(
        self, x, kernel_size: int, dilation: int, stride: int,
    ):
        """This function performs zero-padding on the time axis
        such that their lengths is unchanged after the convolution.
        Arguments
        ---------
        x : torch.Tensor
            Input tensor.
        kernel_size : int
            Size of kernel.
        dilation : int
            Dilation used.
        stride : int
            Stride.
        """

        # Detecting input shape
        L_in = x.shape[-1]

        # Time padding
        padding = get_padding_elem(L_in, stride, kernel_size, dilation)

        # Applying padding
        x = F.pad(x, padding, mode=self.padding_mode)

        return x

    def _check_input_shape(self, shape):
        """Checks the input shape and returns the number of input channels.
        """

        if len(shape) == 2:
            self.unsqueeze = True
            in_channels = 1
        elif self.skip_transpose:
            in_channels = shape[1]
        elif len(shape) == 3:
            in_channels = shape[2]
        else:
            raise ValueError(
                "conv1d expects 2d, 3d inputs. Got " + str(len(shape))
            )

        # Kernel size must be odd
        if self.kernel_size % 2 == 0:
            raise ValueError(
                "The field kernel size must be an odd number. Got %s."
                % (self.kernel_size)
            )
        return in_channels


class Conv2d(nn.Module):
    """This function implements 2d convolution.
    Arguments
    ---------
    out_channels : int
        It is the number of output channels.
    kernel_size : tuple
        Kernel size of the 2d convolutional filters over time and frequency
        axis.
    input_shape : tuple
        The shape of the input. Alternatively use ``in_channels``.
    in_channels : int
        The number of input channels. Alternatively use ``input_shape``.
    stride: int
        Stride factor of the 2d convolutional filters over time and frequency
        axis.
    dilation : int
        Dilation factor of the 2d convolutional filters over time and
        frequency axis.
    padding : str
        (same, valid). If "valid", no padding is performed.
        If "same" and stride is 1, output shape is same as input shape.
    padding_mode : str
        This flag specifies the type of padding. See torch.nn documentation
        for more information.
    groups : int
        This option specifies the convolutional groups. See torch.nn
        documentation for more information.
    bias : bool
        If True, the additive bias b is adopted.
    Example
    -------
    >>> inp_tensor = torch.rand([10, 40, 16, 8])
    >>> cnn_2d = Conv2d(
    ...     input_shape=inp_tensor.shape, out_channels=5, kernel_size=(7, 3)
    ... )
    >>> out_tensor = cnn_2d(inp_tensor)
    >>> out_tensor.shape
    torch.Size([10, 40, 16, 5])
    """

    def __init__(
        self,
        out_channels,
        kernel_size,
        input_shape=None,
        in_channels=None,
        stride=(1, 1),
        dilation=(1, 1),
        padding="same",
        groups=1,
        bias=True,
        padding_mode="reflect",
    ):
        super().__init__()

        # handle the case if some parameter is int
        if isinstance(kernel_size, int):
            kernel_size = (kernel_size, kernel_size)
        if isinstance(stride, int):
            stride = (stride, stride)
        if isinstance(dilation, int):
            dilation = (dilation, dilation)

        self.kernel_size = kernel_size
        self.stride = stride
        self.dilation = dilation
        self.padding = padding
        self.padding_mode = padding_mode
        self.unsqueeze = False

        if input_shape is None and in_channels is None:
            raise ValueError("Must provide one of input_shape or in_channels")

        if in_channels is None:
            in_channels = self._check_input(input_shape)

        # Weights are initialized following pytorch approach
        self.conv = nn.Conv2d(
            in_channels,
            out_channels,
            self.kernel_size,
            stride=self.stride,
            padding=0,
            dilation=self.dilation,
            groups=groups,
            bias=bias,
        )

    def forward(self, x):
        """Returns the output of the convolution.
        Arguments
        ---------
        x : torch.Tensor (batch, time, channel)
            input to convolve. 2d or 4d tensors are expected.
        """
        x = x.transpose(1, -1)
        if self.unsqueeze:
            x = x.unsqueeze(1)

        if self.padding == "same":
            x = self._manage_padding(
                x, self.kernel_size, self.dilation, self.stride
            )

        elif self.padding == "valid":
            pass

        else:
            raise ValueError(
                "Padding must be 'same' or 'valid'. Got " + self.padding
            )

        wx = self.conv(x)

        if self.unsqueeze:
            wx = wx.squeeze(1)
        wx = wx.transpose(1, -1)
        return wx

    def _manage_padding(
        self,
        x,
        kernel_size: Tuple[int, int],
        dilation: Tuple[int, int],
        stride: Tuple[int, int],
    ):
        """This function performs zero-padding on the time and frequency axes
        such that their lengths is unchanged after the convolution.
        Arguments
        ---------
        x : torch.Tensor
        kernel_size : int
        dilation : int
        stride: int
        """
        # Detecting input shape
        L_in = x.shape[-1]

        # Time padding
        padding_time = get_padding_elem(
            L_in, stride[-1], kernel_size[-1], dilation[-1]
        )

        padding_freq = get_padding_elem(
            L_in, stride[-2], kernel_size[-2], dilation[-2]
        )
        padding = padding_time + padding_freq

        # Applying padding
        x = nn.functional.pad(x, padding, mode=self.padding_mode)

        return x

    def _check_input(self, shape):
        """Checks the input shape and returns the number of input channels.
        """

        if len(shape) == 3:
            self.unsqueeze = True
            in_channels = 1

        elif len(shape) == 4:
            in_channels = shape[3]

        else:
            raise ValueError("Expected 3d or 4d inputs. Got " + len(shape))

        # Kernel size must be odd
        if self.kernel_size[0] % 2 == 0 or self.kernel_size[1] % 2 == 0:
            raise ValueError(
                "The field kernel size must be an odd number. Got %s."
                % (self.kernel_size)
            )

        return in_channels


class Conv2dWithConstraint(Conv2d):
    """This function implements 2d convolution with kernel max-norm constaint.
    This corresponds to set an upper bound for the kernel norm.
    Arguments
    ---------
    out_channels : int
        It is the number of output channels.
    kernel_size : tuple
        Kernel size of the 2d convolutional filters over time and frequency
        axis.
    input_shape : tuple
        The shape of the input. Alternatively use ``in_channels``.
    in_channels : int
        The number of input channels. Alternatively use ``input_shape``.
    stride: int
        Stride factor of the 2d convolutional filters over time and frequency
        axis.
    dilation : int
        Dilation factor of the 2d convolutional filters over time and
        frequency axis.
    padding : str
        (same, valid). If "valid", no padding is performed.
        If "same" and stride is 1, output shape is same as input shape.
    padding_mode : str
        This flag specifies the type of padding. See torch.nn documentation
        for more information.
    groups : int
        This option specifies the convolutional groups. See torch.nn
        documentation for more information.
    bias : bool
        If True, the additive bias b is adopted.
    max_norm : float
        kernel  max-norm
    Example
    -------
    >>> inp_tensor = torch.rand([10, 40, 16, 8])
    >>> max_norm = 1
    >>> cnn_2d_constrained = Conv2dWithConstraint(
    ...     in_channels=inp_tensor.shape[-1], out_channels=5, kernel_size=(7, 3)
    ... )
    >>> out_tensor = cnn_2d_constrained(inp_tensor)
    >>> torch.any(torch.norm(cnn_2d_constrained.conv.weight.data, p=2, dim=0)>max_norm)
    tensor(False)
    """

    def __init__(self, *args, max_norm=1, **kwargs):
        self.max_norm = max_norm
        super(Conv2dWithConstraint, self).__init__(*args, **kwargs)

    def forward(self, x):
        """Returns the output of the convolution.
        Arguments
        ---------
        x : torch.Tensor (batch, time, channel)
            input to convolve. 2d or 4d tensors are expected.
        """
        self.conv.weight.data = torch.renorm(
            self.conv.weight.data, p=2, dim=0, maxnorm=self.max_norm
        )
        return super(Conv2dWithConstraint, self).forward(x)


class ConvTranspose1d(nn.Module):
    """This class implements 1d transposed convolution with speechbrain.
    Transpose convolution is normally used to perform upsampling.
    Arguments
    ---------
    out_channels : int
        It is the number of output channels.
    kernel_size : int
        Kernel size of the convolutional filters.
    input_shape : tuple
        The shape of the input. Alternatively use ``in_channels``.
    in_channels : int
        The number of input channels. Alternatively use ``input_shape``.
    stride : int
        Stride factor of the convolutional filters. When the stride factor > 1,
        upsampling in time is performed.
    dilation : int
        Dilation factor of the convolutional filters.
    padding : str or int
        To have in output the target dimension, we suggest tuning the kernel
        size and the padding properly. We also support the following function
        to have some control over the padding and the corresponding ouput
        dimensionality.
        if "valid", no padding is applied
        if "same", padding amount is inferred so that the output size is closest
        to possible to input size. Note that for some kernel_size / stride combinations
        it is not possible to obtain the exact same size, but we return the closest
        possible size.
        if "factor", padding amount is inferred so that the output size is closest
        to inputsize*stride. Note that for some kernel_size / stride combinations
        it is not possible to obtain the exact size, but we return the closest
        possible size.
        if an integer value is entered, a custom padding is used.
    output_padding : int,
        Additional size added to one side of the output shape
    groups: int
        Number of blocked connections from input channels to output channels.
        Default: 1
    bias: bool
        If True, adds a learnable bias to the output
    skip_transpose : bool
        If False, uses batch x time x channel convention of speechbrain.
        If True, uses batch x channel x time convention.
    Example
    -------
    >>> from speechbrain.nnet.CNN import Conv1d, ConvTranspose1d
    >>> inp_tensor = torch.rand([10, 12, 40]) #[batch, time, fea]
    >>> convtranspose_1d = ConvTranspose1d(
    ...     input_shape=inp_tensor.shape, out_channels=8, kernel_size=3, stride=2
    ... )
    >>> out_tensor = convtranspose_1d(inp_tensor)
    >>> out_tensor.shape
    torch.Size([10, 25, 8])
    >>> # Combination of Conv1d and ConvTranspose1d
    >>> from speechbrain.nnet.CNN import Conv1d, ConvTranspose1d
    >>> signal = torch.tensor([1,100])
    >>> signal = torch.rand([1,100]) #[batch, time]
    >>> conv1d = Conv1d(input_shape=signal.shape, out_channels=1, kernel_size=3, stride=2)
    >>> conv_out = conv1d(signal)
    >>> conv_t = ConvTranspose1d(input_shape=conv_out.shape, out_channels=1, kernel_size=3, stride=2, padding=1)
    >>> signal_rec = conv_t(conv_out, output_size=[100])
    >>> signal_rec.shape
    torch.Size([1, 100])
    >>> signal = torch.rand([1,115]) #[batch, time]
    >>> conv_t = ConvTranspose1d(input_shape=signal.shape, out_channels=1, kernel_size=3, stride=2, padding='same')
    >>> signal_rec = conv_t(signal)
    >>> signal_rec.shape
    torch.Size([1, 115])
    >>> signal = torch.rand([1,115]) #[batch, time]
    >>> conv_t = ConvTranspose1d(input_shape=signal.shape, out_channels=1, kernel_size=7, stride=2, padding='valid')
    >>> signal_rec = conv_t(signal)
    >>> signal_rec.shape
    torch.Size([1, 235])
    >>> signal = torch.rand([1,115]) #[batch, time]
    >>> conv_t = ConvTranspose1d(input_shape=signal.shape, out_channels=1, kernel_size=7, stride=2, padding='factor')
    >>> signal_rec = conv_t(signal)
    >>> signal_rec.shape
    torch.Size([1, 231])
    >>> signal = torch.rand([1,115]) #[batch, time]
    >>> conv_t = ConvTranspose1d(input_shape=signal.shape, out_channels=1, kernel_size=3, stride=2, padding=10)
    >>> signal_rec = conv_t(signal)
    >>> signal_rec.shape
    torch.Size([1, 211])
    """

    def __init__(
        self,
        out_channels,
        kernel_size,
        input_shape=None,
        in_channels=None,
        stride=1,
        dilation=1,
        padding=0,
        output_padding=0,
        groups=1,
        bias=True,
        skip_transpose=False,
    ):
        super().__init__()
        self.kernel_size = kernel_size
        self.stride = stride
        self.dilation = dilation
        self.padding = padding
        self.unsqueeze = False
        self.skip_transpose = skip_transpose

        if input_shape is None and in_channels is None:
            raise ValueError("Must provide one of input_shape or in_channels")

        if in_channels is None:
            in_channels = self._check_input_shape(input_shape)

        if self.padding == "same":
            L_in = input_shape[-1] if skip_transpose else input_shape[1]
            padding_value = get_padding_elem_transposed(
                L_in,
                L_in,
                stride=stride,
                kernel_size=kernel_size,
                dilation=dilation,
                output_padding=output_padding,
            )
        elif self.padding == "factor":
            L_in = input_shape[-1] if skip_transpose else input_shape[1]
            padding_value = get_padding_elem_transposed(
                L_in * stride,
                L_in,
                stride=stride,
                kernel_size=kernel_size,
                dilation=dilation,
                output_padding=output_padding,
            )
        elif self.padding == "valid":
            padding_value = 0
        elif type(self.padding) is int:
            padding_value = padding
        else:
            raise ValueError("Not supported padding type")

        self.conv = nn.ConvTranspose1d(
            in_channels,
            out_channels,
            self.kernel_size,
            stride=self.stride,
            dilation=self.dilation,
            padding=padding_value,
            groups=groups,
            bias=bias,
        )

    def forward(self, x, output_size=None):
        """Returns the output of the convolution.
        Arguments
        ---------
        x : torch.Tensor (batch, time, channel)
            input to convolve. 2d or 4d tensors are expected.
        """

        if not self.skip_transpose:
            x = x.transpose(1, -1)

        if self.unsqueeze:
            x = x.unsqueeze(1)

        wx = self.conv(x, output_size=output_size)

        if self.unsqueeze:
            wx = wx.squeeze(1)

        if not self.skip_transpose:
            wx = wx.transpose(1, -1)

        return wx

    def _check_input_shape(self, shape):
        """Checks the input shape and returns the number of input channels.
        """

        if len(shape) == 2:
            self.unsqueeze = True
            in_channels = 1
        elif self.skip_transpose:
            in_channels = shape[1]
        elif len(shape) == 3:
            in_channels = shape[2]
        else:
            raise ValueError(
                "conv1d expects 2d, 3d inputs. Got " + str(len(shape))
            )

        return in_channels


class DepthwiseSeparableConv1d(nn.Module):
    """This class implements the depthwise separable 1d convolution.
    First, a channel-wise convolution is applied to the input
    Then, a point-wise convolution to project the input to output
    Arguments
    ---------
    out_channels : int
        It is the number of output channels.
    kernel_size : int
        Kernel size of the convolutional filters.
    input_shape : tuple
        Expected shape of the input.
    stride : int
        Stride factor of the convolutional filters. When the stride factor > 1,
        a decimation in time is performed.
    dilation : int
        Dilation factor of the convolutional filters.
    padding : str
        (same, valid, causal). If "valid", no padding is performed.
        If "same" and stride is 1, output shape is the same as the input shape.
        "causal" results in causal (dilated) convolutions.
    padding_mode : str
        This flag specifies the type of padding. See torch.nn documentation
        for more information.
    bias : bool
        If True, the additive bias b is adopted.
    Example
    -------
    >>> inp = torch.randn([8, 120, 40])
    >>> conv = DepthwiseSeparableConv1d(256, 3, input_shape=inp.shape)
    >>> out = conv(inp)
    >>> out.shape
    torch.Size([8, 120, 256])
    """

    def __init__(
        self,
        out_channels,
        kernel_size,
        input_shape,
        stride=1,
        dilation=1,
        padding="same",
        bias=True,
    ):
        super().__init__()

        assert len(input_shape) == 3, "input must be a 3d tensor"

        bz, time, chn = input_shape

        self.depthwise = Conv1d(
            chn,
            kernel_size,
            input_shape=input_shape,
            stride=stride,
            dilation=dilation,
            padding=padding,
            groups=chn,
            bias=bias,
        )

        self.pointwise = Conv1d(
            out_channels, kernel_size=1, input_shape=input_shape,
        )

    def forward(self, x):
        """Returns the output of the convolution.
        Arguments
        ---------
        x : torch.Tensor (batch, time, channel)
            input to convolve. 3d tensors are expected.
        """
        return self.pointwise(self.depthwise(x))


class DepthwiseSeparableConv2d(nn.Module):
    """This class implements the depthwise separable 2d convolution.
    First, a channel-wise convolution is applied to the input
    Then, a point-wise convolution to project the input to output
    Arguments
    ---------
    ut_channels : int
        It is the number of output channels.
    kernel_size : int
        Kernel size of the convolutional filters.
    stride : int
        Stride factor of the convolutional filters. When the stride factor > 1,
        a decimation in time is performed.
    dilation : int
        Dilation factor of the convolutional filters.
    padding : str
        (same, valid, causal). If "valid", no padding is performed.
        If "same" and stride is 1, output shape is the same as the input shape.
        "causal" results in causal (dilated) convolutions.
    padding_mode : str
        This flag specifies the type of padding. See torch.nn documentation
        for more information.
    bias : bool
        If True, the additive bias b is adopted.
    Example
    -------
    >>> inp = torch.randn([8, 120, 40, 1])
    >>> conv = DepthwiseSeparableConv2d(256, (3, 3), input_shape=inp.shape)
    >>> out = conv(inp)
    >>> out.shape
    torch.Size([8, 120, 40, 256])
    """

    def __init__(
        self,
        out_channels,
        kernel_size,
        input_shape,
        stride=(1, 1),
        dilation=(1, 1),
        padding="same",
        bias=True,
    ):
        super().__init__()

        # handle the case if some parameter is int
        if isinstance(kernel_size, int):
            kernel_size = (kernel_size, kernel_size)
        if isinstance(stride, int):
            stride = (stride, stride)
        if isinstance(dilation, int):
            dilation = (dilation, dilation)

        assert len(input_shape) in {3, 4}, "input must be a 3d or 4d tensor"
        self.unsqueeze = len(input_shape) == 3

        bz, time, chn1, chn2 = input_shape

        self.depthwise = Conv2d(
            chn2,
            kernel_size,
            input_shape=input_shape,
            stride=stride,
            dilation=dilation,
            padding=padding,
            groups=chn2,
            bias=bias,
        )

        self.pointwise = Conv2d(
            out_channels, kernel_size=(1, 1), input_shape=input_shape,
        )

    def forward(self, x):
        """Returns the output of the convolution.
        Arguments
        ---------
        x : torch.Tensor (batch, time, channel)
            input to convolve. 3d tensors are expected.
        """
        if self.unsqueeze:
            x = x.unsqueeze(1)

        out = self.pointwise(self.depthwise(x))

        if self.unsqueeze:
            out = out.squeeze(1)

        return out


def get_padding_elem(L_in: int, stride: int, kernel_size: int, dilation: int):
    """This function computes the number of elements to add for zero-padding.
    Arguments
    ---------
    L_in : int
    stride: int
    kernel_size : int
    dilation : int
    """
    if stride > 1:
        n_steps = math.ceil(((L_in - kernel_size * dilation) / stride) + 1)
        L_out = stride * (n_steps - 1) + kernel_size * dilation
        padding = [kernel_size // 2, kernel_size // 2]

    else:
        L_out = (L_in - dilation * (kernel_size - 1) - 1) // stride + 1

        padding = [(L_in - L_out) // 2, (L_in - L_out) // 2]
    return padding


def get_padding_elem_transposed(
    L_out: int,
    L_in: int,
    stride: int,
    kernel_size: int,
    dilation: int,
    output_padding: int,
):
    """This function computes the required padding size for transposed convolution
    Arguments
    ---------
    L_out : int
    L_in : int
    stride: int
    kernel_size : int
    dilation : int
    output_padding : int
    """

    padding = -0.5 * (
        L_out
        - (L_in - 1) * stride
        - dilation * (kernel_size - 1)
        - output_padding
        - 1
    )
    return int(padding)

# ============================================Normalization======================================

class BatchNorm1d(nn.Module):
    """Applies 1d batch normalization to the input tensor.
    Arguments
    ---------
    input_shape : tuple
        The expected shape of the input. Alternatively, use ``input_size``.
    input_size : int
        The expected size of the input. Alternatively, use ``input_shape``.
    eps : float
        This value is added to std deviation estimation to improve the numerical
        stability.
    momentum : float
        It is a value used for the running_mean and running_var computation.
    affine : bool
        When set to True, the affine parameters are learned.
    track_running_stats : bool
        When set to True, this module tracks the running mean and variance,
        and when set to False, this module does not track such statistics.
    combine_batch_time : bool
        When true, it combines batch an time axis.
    Example
    -------
    >>> input = torch.randn(100, 10)
    >>> norm = BatchNorm1d(input_shape=input.shape)
    >>> output = norm(input)
    >>> output.shape
    torch.Size([100, 10])
    """

    def __init__(
        self,
        input_shape=None,
        input_size=None,
        eps=1e-05,
        momentum=0.1,
        affine=True,
        track_running_stats=True,
        combine_batch_time=False,
        skip_transpose=False,
    ):
        super().__init__()
        self.combine_batch_time = combine_batch_time
        self.skip_transpose = skip_transpose

        if input_size is None and skip_transpose:
            input_size = input_shape[1]
        elif input_size is None:
            input_size = input_shape[-1]

        self.norm = nn.BatchNorm1d(
            input_size,
            eps=eps,
            momentum=momentum,
            affine=affine,
            track_running_stats=track_running_stats,
        )

    def forward(self, x):
        """Returns the normalized input tensor.
        Arguments
        ---------
        x : torch.Tensor (batch, time, [channels])
            input to normalize. 2d or 3d tensors are expected in input
            4d tensors can be used when combine_dims=True.
        """
        shape_or = x.shape
        if self.combine_batch_time:
            if x.ndim == 3:
                x = x.reshape(shape_or[0] * shape_or[1], shape_or[2])
            else:
                x = x.reshape(
                    shape_or[0] * shape_or[1], shape_or[3], shape_or[2]
                )

        elif not self.skip_transpose:
            x = x.transpose(-1, 1)

        x_n = self.norm(x)

        if self.combine_batch_time:
            x_n = x_n.reshape(shape_or)
        elif not self.skip_transpose:
            x_n = x_n.transpose(1, -1)

        return x_n


class BatchNorm2d(nn.Module):
    """Applies 2d batch normalization to the input tensor.
    Arguments
    ---------
    input_shape : tuple
        The expected shape of the input. Alternatively, use ``input_size``.
    input_size : int
        The expected size of the input. Alternatively, use ``input_shape``.
    eps : float
        This value is added to std deviation estimation to improve the numerical
        stability.
    momentum : float
        It is a value used for the running_mean and running_var computation.
    affine : bool
        When set to True, the affine parameters are learned.
    track_running_stats : bool
        When set to True, this module tracks the running mean and variance,
        and when set to False, this module does not track such statistics.
    Example
    -------
    >>> input = torch.randn(100, 10, 5, 20)
    >>> norm = BatchNorm2d(input_shape=input.shape)
    >>> output = norm(input)
    >>> output.shape
    torch.Size([100, 10, 5, 20])
    """

    def __init__(
        self,
        input_shape=None,
        input_size=None,
        eps=1e-05,
        momentum=0.1,
        affine=True,
        track_running_stats=True,
    ):
        super().__init__()

        if input_shape is None and input_size is None:
            raise ValueError("Expected input_shape or input_size as input")

        if input_size is None:
            input_size = input_shape[-1]

        self.norm = nn.BatchNorm2d(
            input_size,
            eps=eps,
            momentum=momentum,
            affine=affine,
            track_running_stats=track_running_stats,
        )

    def forward(self, x):
        """Returns the normalized input tensor.
        Arguments
        ---------
        x : torch.Tensor (batch, time, channel1, channel2)
            input to normalize. 4d tensors are expected.
        """
        x = x.transpose(-1, 1)
        x_n = self.norm(x)
        x_n = x_n.transpose(1, -1)

        return x_n


class LayerNorm(nn.Module):
    """Applies layer normalization to the input tensor.
    Arguments
    ---------
    input_shape : tuple
        The expected shape of the input.
    eps : float
        This value is added to std deviation estimation to improve the numerical
        stability.
    elementwise_affine : bool
        If True, this module has learnable per-element affine parameters
        initialized to ones (for weights) and zeros (for biases).
    Example
    -------
    >>> input = torch.randn(100, 101, 128)
    >>> norm = LayerNorm(input_shape=input.shape)
    >>> output = norm(input)
    >>> output.shape
    torch.Size([100, 101, 128])
    """

    def __init__(
        self,
        input_size=None,
        input_shape=None,
        eps=1e-05,
        elementwise_affine=True,
    ):
        super().__init__()
        self.eps = eps
        self.elementwise_affine = elementwise_affine

        if input_shape is not None:
            input_size = input_shape[2:]

        self.norm = torch.nn.LayerNorm(
            input_size,
            eps=self.eps,
            elementwise_affine=self.elementwise_affine,
        )

    def forward(self, x):
        """Returns the normalized input tensor.
        Arguments
        ---------
        x : torch.Tensor (batch, time, channels)
            input to normalize. 3d or 4d tensors are expected.
        """
        return self.norm(x)


class InstanceNorm1d(nn.Module):
    """Applies 1d instance normalization to the input tensor.
    Arguments
    ---------
    input_shape : tuple
        The expected shape of the input. Alternatively, use ``input_size``.
    input_size : int
        The expected size of the input. Alternatively, use ``input_shape``.
    eps : float
        This value is added to std deviation estimation to improve the numerical
        stability.
    momentum : float
        It is a value used for the running_mean and running_var computation.
    track_running_stats : bool
        When set to True, this module tracks the running mean and variance,
        and when set to False, this module does not track such statistics.
    affine : bool
        A boolean value that when set to True, this module has learnable
        affine parameters, initialized the same way as done for
        batch normalization. Default: False.
    Example
    -------
    >>> input = torch.randn(100, 10, 20)
    >>> norm = InstanceNorm1d(input_shape=input.shape)
    >>> output = norm(input)
    >>> output.shape
    torch.Size([100, 10, 20])
    """

    def __init__(
        self,
        input_shape=None,
        input_size=None,
        eps=1e-05,
        momentum=0.1,
        track_running_stats=True,
        affine=False,
    ):
        super().__init__()

        if input_shape is None and input_size is None:
            raise ValueError("Expected input_shape or input_size as input")

        if input_size is None:
            input_size = input_shape[-1]

        self.norm = nn.InstanceNorm1d(
            input_size,
            eps=eps,
            momentum=momentum,
            track_running_stats=track_running_stats,
            affine=affine,
        )

    def forward(self, x):
        """Returns the normalized input tensor.
        Arguments
        ---------
        x : torch.Tensor (batch, time, channels)
            input to normalize. 3d tensors are expected.
        """
        x = x.transpose(-1, 1)
        x_n = self.norm(x)
        x_n = x_n.transpose(1, -1)

        return x_n


class InstanceNorm2d(nn.Module):
    """Applies 2d instance normalization to the input tensor.
    Arguments
    ---------
    input_shape : tuple
        The expected shape of the input. Alternatively, use ``input_size``.
    input_size : int
        The expected size of the input. Alternatively, use ``input_shape``.
    eps : float
        This value is added to std deviation estimation to improve the numerical
        stability.
    momentum : float
        It is a value used for the running_mean and running_var computation.
    track_running_stats : bool
        When set to True, this module tracks the running mean and variance,
        and when set to False, this module does not track such statistics.
    affine : bool
        A boolean value that when set to True, this module has learnable
        affine parameters, initialized the same way as done for
        batch normalization. Default: False.
    Example
    -------
    >>> input = torch.randn(100, 10, 20, 2)
    >>> norm = InstanceNorm2d(input_shape=input.shape)
    >>> output = norm(input)
    >>> output.shape
    torch.Size([100, 10, 20, 2])
    """

    def __init__(
        self,
        input_shape=None,
        input_size=None,
        eps=1e-05,
        momentum=0.1,
        track_running_stats=True,
        affine=False,
    ):
        super().__init__()

        if input_shape is None and input_size is None:
            raise ValueError("Expected input_shape or input_size as input")

        if input_size is None:
            input_size = input_shape[-1]

        self.norm = nn.InstanceNorm2d(
            input_size,
            eps=eps,
            momentum=momentum,
            track_running_stats=track_running_stats,
            affine=affine,
        )

    def forward(self, x):
        """Returns the normalized input tensor.
        Arguments
        ---------
        x : torch.Tensor (batch, time, channel1, channel2)
            input to normalize. 4d tensors are expected.
        """
        x = x.transpose(-1, 1)
        x_n = self.norm(x)
        x_n = x_n.transpose(1, -1)

        return x_n
    
# ==============================================pooling==================================================

class StatisticsPooling(torch.nn.Module):
    """
    Mean and Standard deviation pooling
    """
    def __init__(self):
        """

        """
        super(StatisticsPooling, self).__init__()
        pass

    def forward(self, x):
        """

        :param x:
        :return:
        """
        mean = torch.mean(x, dim=2)
        std = torch.std(x, dim=2)
        return torch.cat([mean, std], dim=1)


class GlobalAveragePooling(torch.nn.Module):
    def __init__(self):
        super(GlobalAveragePooling, self).__init__()
        self.pooling = nn.AdaptiveAvgPool1d(1)
    
    # ==========> code <===========
    def forward(self, x):
        '''
        input: (64, 1500, 86)
        output: (64, 1500)
        '''
        pass
    # ==========> code <===========

# from speechbrain.dataio.dataio import length_to_mask

def length_to_mask(length, max_len=None, dtype=None, device=None):
    """Creates a binary mask for each sequence.
    Reference: https://discuss.pytorch.org/t/how-to-generate-variable-length-mask/23397/3
    Arguments
    ---------
    length : torch.LongTensor
        Containing the length of each sequence in the batch. Must be 1D.
    max_len : int
        Max length for the mask, also the size of the second dimension.
    dtype : torch.dtype, default: None
        The dtype of the generated mask.
    device: torch.device, default: None
        The device to put the mask variable.
    Returns
    -------
    mask : tensor
        The binary mask.
    Example
    -------
    >>> length=torch.Tensor([1,2,3])
    >>> mask=length_to_mask(length)
    >>> mask
    tensor([[1., 0., 0.],
            [1., 1., 0.],
            [1., 1., 1.]])
    """
    assert len(length.shape) == 1

    if max_len is None:
        max_len = length.max().long().item()  # using arange to generate mask
    mask = torch.arange(
        max_len, device=length.device, dtype=length.dtype
    ).expand(len(length), max_len) < length.unsqueeze(1)

    if dtype is None:
        dtype = length.dtype

    if device is None:
        device = length.device

    mask = torch.as_tensor(mask, dtype=dtype, device=device)
    return mask


# from speechbrain.nnet.linear import Linear

class Linear(torch.nn.Module):
    """Computes a linear transformation y = wx + b.
    Arguments
    ---------
    n_neurons : int
        It is the number of output neurons (i.e, the dimensionality of the
        output).
    bias : bool
        If True, the additive bias b is adopted.
    combine_dims : bool
        If True and the input is 4D, combine 3rd and 4th dimensions of input.
    Example
    -------
    >>> inputs = torch.rand(10, 50, 40)
    >>> lin_t = Linear(input_shape=(10, 50, 40), n_neurons=100)
    >>> output = lin_t(inputs)
    >>> output.shape
    torch.Size([10, 50, 100])
    """

    def __init__(
        self,
        n_neurons,
        input_shape=None,
        input_size=None,
        bias=True,
        combine_dims=False,
    ):
        super().__init__()
        self.combine_dims = combine_dims

        if input_shape is None and input_size is None:
            raise ValueError("Expected one of input_shape or input_size")

        if input_size is None:
            input_size = input_shape[-1]
            if len(input_shape) == 4 and self.combine_dims:
                input_size = input_shape[2] * input_shape[3]

        # Weights are initialized following pytorch approach
        self.w = nn.Linear(input_size, n_neurons, bias=bias)

    def forward(self, x):
        """Returns the linear transformation of input tensor.
        Arguments
        ---------
        x : torch.Tensor
            Input to transform linearly.
        """
        if x.ndim == 4 and self.combine_dims:
            x = x.reshape(x.shape[0], x.shape[1], x.shape[2] * x.shape[3])

        wx = self.w(x)

        return wx