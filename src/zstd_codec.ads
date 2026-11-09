with Ada.Streams;

package Zstd_Codec is
   Codec_Error : exception;

   --  Format/reference ceiling, not a process memory allowance.
   Max_Frame_Content_Size : constant Ada.Streams.Stream_Element_Count :=
     1_073_741_824;

   function Compress
     (Data : Ada.Streams.Stream_Element_Array)
      return Ada.Streams.Stream_Element_Array;
   --  One frame: level 3, workers 0, known size, checksum, no dictionary.

   function Frame_Content_Size
     (Data : Ada.Streams.Stream_Element_Array)
      return Ada.Streams.Stream_Element_Count;
   --  Require one exact regular frame with a checksum and a known bounded
   --  content size. Decompress performs the content/checksum verification.

   function Decompress
     (Data          : Ada.Streams.Stream_Element_Array;
      Expected_Size : Ada.Streams.Stream_Element_Count)
      return Ada.Streams.Stream_Element_Array;
   --  Check the frame and Expected_Size before allocating decoded storage.
end Zstd_Codec;
