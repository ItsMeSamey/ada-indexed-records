with Ada.Streams;
with Ada.Unchecked_Deallocation;
with Interfaces.C;
with Interfaces.C.Strings;
with System;

package body Zstd_Codec is
   use Ada.Streams;
   use type Interfaces.C.size_t;
   use type Interfaces.C.unsigned;
   use type Interfaces.C.unsigned_long_long;
   use type Interfaces.C.Strings.chars_ptr;
   use type System.Address;

   pragma Linker_Options ("-lzstd");

   subtype C_Size is Interfaces.C.size_t;
   subtype C_Unsigned_Long_Long is Interfaces.C.unsigned_long_long;

   --  Stable ZSTD_cParameter values from the installed zstd.h 1.5.7.
   Compression_Level : constant Interfaces.C.int := 100;
   Content_Size_Flag : constant Interfaces.C.int := 200;
   Checksum_Flag     : constant Interfaces.C.int := 201;
   Dictionary_Flag   : constant Interfaces.C.int := 202;
   Worker_Count      : constant Interfaces.C.int := 400;

   function C_Create_Context return System.Address
     with Import, Convention => C, External_Name => "ZSTD_createCCtx";

   function C_Free_Context (Context : System.Address) return C_Size
     with Import, Convention => C, External_Name => "ZSTD_freeCCtx";

   function C_Set_Parameter
     (Context   : System.Address;
      Parameter : Interfaces.C.int;
      Value     : Interfaces.C.int) return C_Size
     with Import, Convention => C,
       External_Name => "ZSTD_CCtx_setParameter";

   function C_Compress_Bound (Source_Size : C_Size) return C_Size
     with Import, Convention => C, External_Name => "ZSTD_compressBound";

   function C_Compress
     (Context              : System.Address;
      Destination          : System.Address;
      Destination_Capacity : C_Size;
      Source               : System.Address;
      Source_Size          : C_Size) return C_Size
     with Import, Convention => C, External_Name => "ZSTD_compress2";

   function C_Decompress
     (Destination          : System.Address;
      Destination_Capacity : C_Size;
      Source               : System.Address;
      Source_Size          : C_Size) return C_Size
     with Import, Convention => C, External_Name => "ZSTD_decompress";

   function C_Frame_Content_Size
     (Source      : System.Address;
      Source_Size : C_Size) return C_Unsigned_Long_Long
     with Import, Convention => C,
       External_Name => "ZSTD_getFrameContentSize";

   function C_Frame_Compressed_Size
     (Source      : System.Address;
      Source_Size : C_Size) return C_Size
     with Import, Convention => C,
       External_Name => "ZSTD_findFrameCompressedSize";

   function C_Is_Error (Value : C_Size) return Interfaces.C.unsigned
     with Import, Convention => C, External_Name => "ZSTD_isError";

   function C_Error_Name
     (Value : C_Size) return Interfaces.C.Strings.chars_ptr
     with Import, Convention => C, External_Name => "ZSTD_getErrorName";

   type Buffer_Access is access Stream_Element_Array;
   procedure Release is new Ada.Unchecked_Deallocation
     (Object => Stream_Element_Array, Name => Buffer_Access);

   procedure Check_Byte_ABI is
   begin
      if Stream_Element'Size /= 8 or else Interfaces.C.char'Size /= 8 then
         raise Codec_Error with "unsupported byte ABI";
      end if;
   end Check_Byte_ABI;

   procedure Check (Value : C_Size; Operation : String) is
      Name : Interfaces.C.Strings.chars_ptr;
   begin
      if C_Is_Error (Value) /= 0 then
         Name := C_Error_Name (Value);
         if Name = Interfaces.C.Strings.Null_Ptr then
            raise Codec_Error with Operation & ": zstd error";
         end if;
         raise Codec_Error with
           Operation & ": " & Interfaces.C.Strings.Value (Name);
      end if;
   end Check;

   function Ada_Count (Value : C_Size) return Stream_Element_Count is
   begin
      if C_Unsigned_Long_Long (Value) >
        C_Unsigned_Long_Long (Stream_Element_Count'Last)
      then
         raise Codec_Error with "compressed size exceeds Ada array bounds";
      end if;
      return Stream_Element_Count (Value);
   end Ada_Count;

   procedure Close_Context (Context : in out System.Address) is
      Status : C_Size;
   begin
      if Context /= System.Null_Address then
         Status := C_Free_Context (Context);
         Context := System.Null_Address;
         Check (Status, "free compression context");
      end if;
   end Close_Context;

   procedure Abandon_Context (Context : in out System.Address) is
   begin
      if Context /= System.Null_Address then
         declare
            Ignored : constant C_Size := C_Free_Context (Context);
            pragma Unreferenced (Ignored);
         begin
            Context := System.Null_Address;
         end;
      end if;
   end Abandon_Context;

   procedure Check_Header (Data : Stream_Element_Array) is
      First      : constant Stream_Element_Offset := Data'First;
      Descriptor : Stream_Element;
   begin
      Check_Byte_ABI;
      if Data'Length < 5 then
         raise Codec_Error with "truncated zstd frame header";
      end if;
      if Data (First) /= 16#28#
        or else Data (First + 1) /= 16#B5#
        or else Data (First + 2) /= 16#2F#
        or else Data (First + 3) /= 16#FD#
      then
         raise Codec_Error with "expected a regular zstd frame";
      end if;
      Descriptor := Data (First + 4);
      --  Frame header descriptor: checksum bit2, reserved bit3, dictID bits0–1.
      if (Descriptor and 16#04#) = 0 then
         raise Codec_Error with "zstd content checksum is required";
      end if;
      if (Descriptor and 16#08#) /= 0 then
         raise Codec_Error with "reserved zstd frame header bit";
      end if;
      if (Descriptor and 16#03#) /= 0 then
         raise Codec_Error with "dictionary frame is unsupported";
      end if;
   end Check_Header;

   function Frame_Content_Size
     (Data : Stream_Element_Array) return Stream_Element_Count
   is
      Compressed_Size : C_Size;
      Decoded_Size    : C_Unsigned_Long_Long;
   begin
      Check_Header (Data);
      Compressed_Size := C_Frame_Compressed_Size
        (Data (Data'First)'Address, C_Size (Data'Length));
      Check (Compressed_Size, "read compressed frame size");
      if Compressed_Size /= C_Size (Data'Length) then
         raise Codec_Error with "compressed frame size mismatch";
      end if;
      Decoded_Size := C_Frame_Content_Size
        (Data (Data'First)'Address, C_Size (Data'Length));
      if Decoded_Size = C_Unsigned_Long_Long'Last then
         raise Codec_Error with "zstd content size is unknown";
      elsif Decoded_Size = C_Unsigned_Long_Long'Last - 1 then
         raise Codec_Error with "invalid zstd content size";
      elsif Decoded_Size > C_Unsigned_Long_Long (Max_Frame_Content_Size) then
         raise Codec_Error with "zstd content size exceeds the frame ceiling";
      end if;
      return Stream_Element_Count (Decoded_Size);
   end Frame_Content_Size;

   function Compress
     (Data : Stream_Element_Array) return Stream_Element_Array
   is
      Context  : System.Address := System.Null_Address;
      Buffer   : Buffer_Access := null;
      Dummy    : aliased Stream_Element := 0;
      Source   : System.Address;
      Capacity : C_Size;
      Used     : C_Size;
   begin
      Check_Byte_ABI;
      if Data'Length > Max_Frame_Content_Size then
         raise Codec_Error with "input exceeds the frame ceiling";
      end if;
      Source := (if Data'Length = 0 then Dummy'Address
                 else Data (Data'First)'Address);
      Capacity := C_Compress_Bound (C_Size (Data'Length));
      Check (Capacity, "compute compression bound");
      if Capacity = 0 then
         raise Codec_Error with "zero compression bound";
      end if;
      Buffer := new Stream_Element_Array (1 .. Ada_Count (Capacity));
      Context := C_Create_Context;
      if Context = System.Null_Address then
         raise Codec_Error with "compression context allocation failed";
      end if;
      Check (C_Set_Parameter (Context, Compression_Level, 3), "set compression level");
      Check (C_Set_Parameter (Context, Worker_Count, 0), "set worker count");
      Check (C_Set_Parameter (Context, Content_Size_Flag, 1), "enable content size");
      Check (C_Set_Parameter (Context, Checksum_Flag, 1), "enable content checksum");
      Check (C_Set_Parameter (Context, Dictionary_Flag, 0), "disable dictionary ID");
      Used := C_Compress
        (Context, Buffer (Buffer.all'First)'Address, Capacity,
         Source, C_Size (Data'Length));
      Check (Used, "compress frame");
      if Used = 0 or else Used > Capacity then
         raise Codec_Error with "invalid compressed output size";
      end if;
      Close_Context (Context);
      declare
         Count : constant Stream_Element_Count := Ada_Count (Used);
      begin
         if Frame_Content_Size (Buffer.all (1 .. Count)) /= Data'Length then
            raise Codec_Error with "compressed content size mismatch";
         end if;
         return Result : Stream_Element_Array (1 .. Count) do
            Result := Buffer.all (1 .. Count);
            Release (Buffer);
         end return;
      end;
   exception
      when others =>
         Abandon_Context (Context);
         Release (Buffer);
         raise;
   end Compress;

   function Decompress
     (Data          : Stream_Element_Array;
      Expected_Size : Stream_Element_Count) return Stream_Element_Array
   is
      Buffer : Buffer_Access := null;
      Used   : C_Size;
   begin
      if Expected_Size > Max_Frame_Content_Size then
         raise Codec_Error with "expected size exceeds the frame ceiling";
      end if;
      if Frame_Content_Size (Data) /= Expected_Size then
         raise Codec_Error with "decoded frame size differs from expected size";
      end if;
      --  A one-byte allocation gives the C call a valid pointer even at size0.
      Buffer := new Stream_Element_Array
        (1 .. Stream_Element_Count'Max (Expected_Size, 1));
      Used := C_Decompress
        (Buffer (Buffer.all'First)'Address, C_Size (Expected_Size),
         Data (Data'First)'Address, C_Size (Data'Length));
      Check (Used, "decompress frame");
      if Used /= C_Size (Expected_Size) then
         raise Codec_Error with "decompressed size mismatch";
      end if;
      return Result : Stream_Element_Array (1 .. Expected_Size) do
         if Expected_Size /= 0 then
            Result := Buffer.all (1 .. Expected_Size);
         end if;
         Release (Buffer);
      end return;
   exception
      when others =>
         Release (Buffer);
         raise;
   end Decompress;
end Zstd_Codec;
